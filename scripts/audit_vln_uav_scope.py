"""Audit UAV-focused papers and repositories currently visible in the VLN domain."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from packages.domain.exporters.common import atomic_write_json
from packages.domain.models import Asset, Relation

DOMAIN = "vision-language-navigation"
UAV_TITLE_PATTERN = re.compile(
    r"(?:\bUAVs?\b|\bAerial\b|\bDrones?\b|\bQuadrotor\b|\bFlights?\b|"
    r"\b(?:Dream|Recover|World|Dyn|Auto|On)Fly\b|\bFly0\b|\bAero(?:Act|DPO)\b|"
    r"\bAirForesight\b|\bImagineUAV\b|\bThink Like a Pilot\b)",
    re.IGNORECASE,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "vln-uav-scope-audit.json",
    )
    return parser.parse_args()


def build_audit() -> dict[str, Any]:
    database = DatabaseManager(get_settings())
    try:
        with database.session() as session:
            assets = list(
                session.scalars(
                    select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.id)
                ).all()
            )
            by_id = {item.id: item for item in assets}
            papers = [
                item
                for item in assets
                if item.asset_type == "paper"
                and DOMAIN in (item.domain_slugs or [])
                and UAV_TITLE_PATTERN.search(item.title)
            ]
            paper_ids = {item.id for item in papers}
            relations = list(
                session.scalars(
                    select(Relation).where(
                        Relation.deleted_at.is_(None),
                        Relation.relation_type == "implemented_by",
                    )
                ).all()
            )

        repository_ids = {
            relation.object_asset_id
            for relation in relations
            if relation.subject_asset_id in paper_ids
            and relation.object_asset_id in by_id
            and by_id[relation.object_asset_id].asset_type == "repository"
        }
        repository_items: list[dict[str, Any]] = []
        for repository_id in sorted(repository_ids, key=str):
            repository = by_id[repository_id]
            linked_paper_ids = {
                relation.subject_asset_id
                for relation in relations
                if relation.object_asset_id == repository_id
                and relation.subject_asset_id in by_id
                and by_id[relation.subject_asset_id].asset_type == "paper"
            }
            non_uav_vln = [
                by_id[paper_id]
                for paper_id in linked_paper_ids - paper_ids
                if DOMAIN in (by_id[paper_id].domain_slugs or [])
            ]
            repository_items.append(
                {
                    "asset_id": str(repository.id),
                    "title": repository.title,
                    "currently_in_vln": DOMAIN in (repository.domain_slugs or []),
                    "linked_uav_papers": sorted(str(item) for item in linked_paper_ids & paper_ids),
                    "linked_non_uav_vln_papers": [
                        {"asset_id": str(item.id), "title": item.title}
                        for item in sorted(non_uav_vln, key=lambda value: value.title)
                    ],
                    "exclude_from_vln": not non_uav_vln,
                }
            )

        paper_items = [
            {
                "asset_id": str(item.id),
                "title": item.title,
                "year": item.year,
                "first_public_date": (item.attributes or {}).get("first_public_date"),
                "source_urls": list(item.source_urls or []),
            }
            for item in sorted(
                papers,
                key=lambda value: (
                    str((value.attributes or {}).get("first_public_date") or ""),
                    value.title,
                ),
                reverse=True,
            )
        ]
        return {
            "schema": "research-asset-hub.domain-scope-audit/v1",
            "status": "passed",
            "domain": DOMAIN,
            "policy": "exclude-uav-focused-navigation",
            "paper_count_before": sum(
                item.asset_type == "paper" and DOMAIN in (item.domain_slugs or [])
                for item in assets
            ),
            "excluded_paper_count": len(paper_items),
            "paper_count_after": sum(
                item.asset_type == "paper" and DOMAIN in (item.domain_slugs or [])
                for item in assets
            )
            - len(paper_items),
            "papers": paper_items,
            "repositories": repository_items,
            "repository_exclusion_count": sum(
                item["currently_in_vln"] and item["exclude_from_vln"]
                for item in repository_items
            ),
        }
    finally:
        database.dispose()


def main() -> int:
    arguments = _arguments()
    report = build_audit()
    atomic_write_json(arguments.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "paper_count_before": report["paper_count_before"],
                "excluded_paper_count": report["excluded_paper_count"],
                "paper_count_after": report["paper_count_after"],
                "repository_exclusion_count": report["repository_exclusion_count"],
                "report": str(arguments.report),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
