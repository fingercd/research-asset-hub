"""Seed idempotent bootstrap extraction and indexing jobs."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter

from sqlalchemy import select

from apps.api.config import get_settings
from apps.api.database import DatabaseManager
from packages.domain.enums import JobStatus
from packages.domain.models import Asset, Job, Representation, Source


def _representation_fingerprint(representation: Representation) -> str:
    value = (
        representation.sha256
        or representation.tree_digest
        or representation.revision
        or representation.path_ref
        or str(representation.id)
    )
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def seed_jobs(
    database: DatabaseManager,
    *,
    include_assets: bool,
    include_representations: bool,
    max_representations: int | None,
    representation_types: frozenset[str] | None = None,
    generation: str = "v1",
    source_alias: str | None = None,
) -> Counter[str]:
    if source_alias is not None and (include_assets or not include_representations):
        raise ValueError("--source requires --only representations.")
    counters: Counter[str] = Counter()
    with database.session() as session:
        source_id = None
        if source_alias is not None:
            source_id = session.scalar(select(Source.id).where(Source.alias == source_alias))
            if source_id is None:
                raise ValueError(f"Unknown source alias: {source_alias!r}.")
        if include_assets:
            assets = list(
                session.scalars(
                    select(Asset)
                    .where(Asset.deleted_at.is_(None))
                    .order_by(Asset.id)
                ).all()
            )
            for asset in assets:
                dedupe = f"bootstrap:v1:asset-index:{asset.id}"
                existing = session.scalar(
                    select(Job.id).where(
                        Job.job_type == "index_asset",
                        Job.dedupe_key == dedupe,
                    )
                )
                if existing is not None:
                    counters["asset_jobs_reused"] += 1
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
                .where(
                    Representation.deleted_at.is_(None),
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
                    select(Job.id).where(
                        Job.job_type == "extract_representation",
                        Job.dedupe_key == dedupe,
                    )
                )
                if existing is not None:
                    counters["representation_jobs_reused"] += 1
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
        help="Exact source alias; requires --only representations.",
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
    if args.source is not None and args.only != "representations":
        parser.error("--source requires --only representations.")
    representation_types = frozenset(
        item.strip()
        for item in args.types.split(",")
        if item.strip()
    )
    try:
        counters = seed_jobs(
            DatabaseManager(get_settings()),
            include_assets=args.only in {"assets", "all"},
            include_representations=args.only in {"representations", "all"},
            max_representations=args.max_representations,
            representation_types=representation_types or None,
            generation=args.generation,
            source_alias=args.source,
        )
    except ValueError as error:
        parser.error(str(error))
    for key, value in sorted(counters.items()):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
