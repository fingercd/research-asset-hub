"""Inventory active VLN papers and their local document representations."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.exporters.common import atomic_write_json
from packages.domain.models import Asset, ExternalIdentifierModel, Representation, Source
from packages.domain.scope_policy import DomainScopePolicy
from packages.domain.serving_scope import active_representation_clause

DOMAIN = "vision-language-navigation"
EXPECTED_PAPER_COUNT = 109
DOCUMENT_TYPES = frozenset({"analysis", "guide", "html", "html_document"})


def _scope_audit(rows: Sequence[Mapping[str, Any]], policy: DomainScopePolicy) -> dict[str, Any]:
    matches = [
        {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "rule_id": rule.rule_id,
            "asset_id_match": str(row["asset_id"]) in rule.asset_ids,
            "title_match": any(
                pattern.search(str(row["title"])) for pattern in rule.title_patterns
            ),
        }
        for row in rows
        for rule in policy.rules
        if rule.domain == DOMAIN
        and rule.matches(asset_id=UUID(str(row["asset_id"])), title=str(row["title"]))
    ]
    return {
        "status": "failed" if matches else "passed",
        "match_count": len(matches),
        "matches": matches,
    }


def _asset_id_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    asset_ids = sorted({str(row["asset_id"]) for row in rows})
    return hashlib.sha256("\n".join(asset_ids).encode("utf-8")).hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "mineru" / "vln-20260904" / "inventory.json",
    )
    return parser.parse_args()


def _local_state(
    registry: SourceRegistry,
    source_alias: str | None,
    root_key: str,
    path_ref: str | None,
) -> str:
    if source_alias is None or path_ref is None:
        return "external_or_unresolved"
    try:
        path = registry.resolve(source_alias, root_key, path_ref)
    except UnsafeAssetPath:
        return "unsafe"
    if path.is_file():
        return "file"
    if path.is_dir():
        return "directory"
    return "missing"


def _representation_payload(
    item: Representation,
    source_alias: str | None,
    registry: SourceRegistry,
) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "type": item.representation_type,
        "role": item.role,
        "availability": item.availability,
        "source_alias": source_alias,
        "root_key": item.root_key,
        "path_ref": item.path_ref,
        "external": item.external_url is not None,
        "network_required": item.network_required,
        "local_state": _local_state(registry, source_alias, item.root_key, item.path_ref),
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
    }


def _pdf_priority(item: dict[str, Any]) -> tuple[int, int, int, str]:
    source_alias = str(item.get("source_alias") or "")
    if source_alias.startswith("vln-2026-"):
        source_rank = 0
    elif source_alias.endswith("-v2"):
        source_rank = 1
    elif "-core-" in source_alias:
        source_rank = 2
    elif source_alias == "vln-atlas-20260726":
        source_rank = 3
    else:
        source_rank = 4
    return (
        0 if item["local_state"] == "file" else 1,
        0 if item["role"] == "original" else 1,
        source_rank,
        str(item["id"]),
    )


def build_inventory() -> dict[str, Any]:
    scope_path = PROJECT_ROOT / "config" / "domain-scope-exclusions.yaml"
    # The current inventory must prove the explicit exclusion policy was loaded.
    scope_sha256 = hashlib.sha256(scope_path.read_bytes()).hexdigest()
    scope_policy = DomainScopePolicy.load(scope_path)
    settings = get_settings()
    database = DatabaseManager(settings)
    registry = SourceRegistry.load(settings.sources_config_path)
    try:
        with database.session() as session:
            papers = [
                item
                for item in session.scalars(
                    select(Asset)
                    .where(Asset.deleted_at.is_(None), Asset.asset_type == "paper")
                    .order_by(Asset.title, Asset.id)
                ).all()
                if DOMAIN in (item.domain_slugs or [])
            ]
            paper_ids = {item.id for item in papers}
            representations = list(
                session.scalars(
                    select(Representation)
                    .where(
                        Representation.asset_id.in_(paper_ids),
                        Representation.deleted_at.is_(None),
                        active_representation_clause(),
                    )
                    .order_by(Representation.asset_id, Representation.id)
                ).all()
            )
            source_ids = {item.source_id for item in representations if item.source_id is not None}
            aliases = {
                item.id: item.alias
                for item in session.scalars(select(Source).where(Source.id.in_(source_ids))).all()
            }
            identifiers: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
            for identifier in session.scalars(
                select(ExternalIdentifierModel)
                .where(ExternalIdentifierModel.asset_id.in_(paper_ids))
                .order_by(
                    ExternalIdentifierModel.asset_id,
                    ExternalIdentifierModel.scheme,
                    ExternalIdentifierModel.canonical,
                )
            ):
                identifiers[identifier.asset_id].append(
                    {
                        "scheme": identifier.scheme,
                        "scope": identifier.scope,
                        "value": identifier.value,
                        "canonical": identifier.canonical,
                        "is_identity": identifier.is_identity,
                    }
                )

        by_asset: dict[UUID, list[Representation]] = defaultdict(list)
        for representation in representations:
            by_asset[representation.asset_id].append(representation)

        rows: list[dict[str, Any]] = []
        source_counts: Counter[str] = Counter()
        for paper in papers:
            payloads = [
                _representation_payload(
                    item,
                    aliases.get(item.source_id) if item.source_id is not None else None,
                    registry,
                )
                for item in by_asset.get(paper.id, [])
            ]
            for alias in {
                str(item["source_alias"]) for item in payloads if item["source_alias"] is not None
            }:
                source_counts[alias] += 1
            pdfs = sorted(
                (item for item in payloads if item["type"] == "pdf"),
                key=_pdf_priority,
            )
            documents = [item for item in payloads if item["type"] in DOCUMENT_TYPES]
            rows.append(
                {
                    "asset_id": str(paper.id),
                    "title": paper.title,
                    "title_zh": paper.title_zh,
                    "authors": list(paper.authors or []),
                    "year": paper.year,
                    "venue": paper.venue,
                    "abstract": paper.abstract,
                    "summary": paper.summary,
                    "tags": list(paper.tags or []),
                    "source_urls": list(paper.source_urls or []),
                    "attributes": dict(paper.attributes or {}),
                    "identifiers": identifiers.get(paper.id, []),
                    "primary_pdf": pdfs[0] if pdfs else None,
                    "pdf_candidates": pdfs,
                    "existing_documents": documents,
                    "all_representations": payloads,
                }
            )

        missing_pdf = [
            item["asset_id"]
            for item in rows
            if not item["primary_pdf"] or item["primary_pdf"]["local_state"] != "file"
        ]
        document_counts = Counter(
            representation["type"]
            for item in rows
            for representation in item["existing_documents"]
            if representation["local_state"] == "file"
        )
        with_documents = sum(
            any(
                representation["local_state"] == "file"
                for representation in item["existing_documents"]
            )
            for item in rows
        )
        scope_audit = _scope_audit(rows, scope_policy)
        status = (
            "passed"
            if len(rows) == EXPECTED_PAPER_COUNT
            and not missing_pdf
            and scope_audit["status"] == "passed"
            else "failed"
        )
        return {
            "schema": "research-asset-hub.vln-unified-guide-inventory/v1",
            "status": status,
            "domain": DOMAIN,
            "expected_paper_count": EXPECTED_PAPER_COUNT,
            "paper_count": len(rows),
            "asset_id_set_sha256": _asset_id_digest(rows),
            "scope_exclusions": {
                "config_path": "config/domain-scope-exclusions.yaml",
                "config_sha256": scope_sha256,
                **scope_audit,
            },
            "papers_with_local_pdf": len(rows) - len(missing_pdf),
            "papers_missing_local_pdf": missing_pdf,
            "papers_with_existing_documents": with_documents,
            "papers_without_existing_documents": len(rows) - with_documents,
            "existing_document_counts": dict(sorted(document_counts.items())),
            "source_coverage": dict(sorted(source_counts.items())),
            "items": rows,
        }
    finally:
        database.dispose()


def main() -> int:
    arguments = _arguments()
    report = build_inventory()
    atomic_write_json(arguments.report, report)
    print(
        {
            "status": report["status"],
            "paper_count": report["paper_count"],
            "papers_with_local_pdf": report["papers_with_local_pdf"],
            "papers_with_existing_documents": report["papers_with_existing_documents"],
            "report": str(arguments.report),
        }
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
