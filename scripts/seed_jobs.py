"""Seed idempotent bootstrap extraction and indexing jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.database import DatabaseManager
from packages.domain.enums import JobStatus
from packages.domain.models import (
    Annotation,
    Asset,
    ExternalIdentifierModel,
    Job,
    ReadingState,
    Representation,
    Source,
)
from packages.domain.serving_scope import active_representation_clause
from packages.indexing.builder import resolve_asset_scope


def _representation_fingerprint(representation: Representation) -> str:
    value = (
        representation.sha256
        or representation.tree_digest
        or representation.revision
        or representation.path_ref
        or str(representation.id)
    )
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def _asset_index_fingerprint(session: Session, asset: Asset) -> str:
    """Include changes that trigger the shared asset-level keyword projection."""

    representations = session.execute(
        select(
            Representation.id,
            Representation.updated_at,
            Representation.sha256,
            Representation.tree_digest,
            Representation.revision,
        )
        .where(
            Representation.asset_id == asset.id,
            Representation.deleted_at.is_(None),
            active_representation_clause(),
        )
        .order_by(Representation.id)
    ).all()
    annotations = session.execute(
        select(Annotation.id, Annotation.updated_at)
        .where(
            Annotation.asset_id == asset.id,
            Annotation.deleted_at.is_(None),
        )
        .order_by(Annotation.id)
    ).all()
    reading_states = session.execute(
        select(ReadingState.user_id, ReadingState.updated_at)
        .where(
            ReadingState.asset_id == asset.id,
        )
        .order_by(ReadingState.user_id)
    ).all()
    identifiers = session.execute(
        select(
            ExternalIdentifierModel.canonical,
            ExternalIdentifierModel.value,
        )
        .where(ExternalIdentifierModel.asset_id == asset.id)
        .order_by(
            ExternalIdentifierModel.canonical,
            ExternalIdentifierModel.value,
        )
    ).all()
    payload = [
        str(asset.updated_at),
        [list(row) for row in representations],
        [list(row) for row in annotations],
        [list(row) for row in reading_states],
        [list(row) for row in identifiers],
    ]
    encoded = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _count_existing(counters: Counter[str], kind: str, job: Job) -> None:
    if job.status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
        counters[f"{kind}_jobs_{job.status}"] += 1
    else:
        counters[f"{kind}_jobs_reused"] += 1


def seed_jobs(
    database: DatabaseManager,
    *,
    include_assets: bool,
    include_representations: bool,
    max_representations: int | None,
    representation_types: frozenset[str] | None = None,
    generation: str = "v1",
    source_alias: str | None = None,
    asset_ids: Iterable[UUID] | None = None,
) -> Counter[str]:
    counters: Counter[str] = Counter()
    with database.session() as session:
        selected_assets = resolve_asset_scope(
            session,
            asset_ids=asset_ids,
            source_alias=source_alias,
        )
        source_id = None
        if source_alias is not None:
            source_id = session.scalar(select(Source.id).where(Source.alias == source_alias))
        if include_assets:
            asset_statement = select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.id)
            if selected_assets is not None:
                asset_statement = asset_statement.where(Asset.id.in_(selected_assets))
            assets = list(session.scalars(asset_statement).all())
            for asset in assets:
                dedupe = (
                    f"bootstrap:{generation}:asset-index:{asset.id}:"
                    f"{_asset_index_fingerprint(session, asset)}"
                )
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "index_asset",
                        Job.dedupe_key == dedupe,
                    )
                )
                if existing is not None:
                    _count_existing(counters, "asset", existing)
                    continue
                session.add(
                    Job(
                        job_type="index_asset",
                        parameters={"asset_id": str(asset.id)},
                        status=JobStatus.PENDING.value,
                        priority=100,
                        dedupe_key=dedupe,
                    )
                )
                counters["asset_jobs_created"] += 1

        if include_representations:
            statement = (
                select(Representation)
                .join(Asset, Asset.id == Representation.asset_id)
                .where(
                    Asset.deleted_at.is_(None),
                    Representation.deleted_at.is_(None),
                    active_representation_clause(),
                    Representation.availability == "available",
                    Representation.source_id.is_not(None),
                    Representation.path_ref.is_not(None),
                    Representation.materialization_kind != "url",
                )
                .order_by(Representation.representation_type, Representation.id)
            )
            if representation_types:
                statement = statement.where(
                    Representation.representation_type.in_(representation_types)
                )
            if source_id is not None:
                statement = statement.where(Representation.source_id == source_id)
            if selected_assets is not None:
                statement = statement.where(Representation.asset_id.in_(selected_assets))
            if max_representations is not None:
                statement = statement.limit(max_representations)
            representations = list(session.scalars(statement).all())
            priorities = {
                "pdf": 80,
                "html": 70,
                "guide": 60,
                "markdown": 60,
                "analysis": 50,
                "annotation": 50,
                "repository": 40,
            }
            for representation in representations:
                fingerprint = _representation_fingerprint(representation)
                dedupe = (
                    f"bootstrap:{generation}:representation-extract:"
                    f"{representation.id}:{fingerprint}"
                )
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "extract_representation",
                        Job.dedupe_key == dedupe,
                    )
                )
                if existing is not None:
                    _count_existing(counters, "representation", existing)
                    continue
                session.add(
                    Job(
                        job_type="extract_representation",
                        parameters={"representation_id": str(representation.id)},
                        status=JobStatus.PENDING.value,
                        priority=priorities.get(representation.representation_type, 20),
                        max_attempts=3,
                        dedupe_key=dedupe,
                    )
                )
                counters["representation_jobs_created"] += 1
    return counters


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=("assets", "representations", "all"),
        default="all",
    )
    parser.add_argument("--max-representations", type=int, default=None)
    parser.add_argument(
        "--source",
        default=None,
        help="Exact source alias; includes primary and materialized repository assets.",
    )
    parser.add_argument(
        "--asset-id",
        action="append",
        type=UUID,
        default=None,
        help="Exact asset UUID; repeat for multiple assets.",
    )
    parser.add_argument(
        "--types",
        default="",
        help="Comma-separated representation types to extract.",
    )
    parser.add_argument(
        "--generation",
        default="v1",
        help="Dedupe generation; bump only when parser semantics change.",
    )
    args = parser.parse_args()
    representation_types = frozenset(item.strip() for item in args.types.split(",") if item.strip())
    try:
        counters = seed_jobs(
            DatabaseManager(get_settings()),
            include_assets=args.only in {"assets", "all"},
            include_representations=args.only in {"representations", "all"},
            max_representations=args.max_representations,
            representation_types=representation_types or None,
            generation=args.generation,
            source_alias=args.source,
            asset_ids=args.asset_id,
        )
    except ValueError as error:
        parser.error(str(error))
    for key, value in sorted(counters.items()):
        print(f"{key}={value}")
    return 1 if any(key.endswith(("_failed", "_cancelled")) for key in counters) else 0


if __name__ == "__main__":
    raise SystemExit(main())
