"""Generate reviewable Zotero import packages from the canonical paper assets."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from packages.domain.models import Asset, ExternalIdentifierModel
from packages.integrations import export_bibtex, export_csl_json


def build_assets(database: DatabaseManager) -> list[dict[str, Any]]:
    with database.session() as session:
        papers = list(
            session.scalars(
                select(Asset)
                .where(
                    Asset.asset_type == "paper",
                    Asset.deleted_at.is_(None),
                )
                .order_by(Asset.title, Asset.id)
            ).all()
        )
        identifiers: dict[Any, dict[str, str]] = defaultdict(dict)
        for identifier in session.scalars(select(ExternalIdentifierModel)).all():
            identifiers[identifier.asset_id][identifier.scheme] = identifier.canonical

        return [
            {
                "id": str(asset.id),
                "title": asset.title,
                "authors": list(asset.authors or []),
                "year": asset.year,
                "venue": asset.venue,
                "abstract": asset.abstract,
                "doi": identifiers[asset.id].get("doi"),
                "url": next(iter(asset.source_urls or []), None),
            }
            for asset in papers
        ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "zotero",
    )
    arguments = parser.parse_args()
    destination = arguments.output.resolve()
    database = DatabaseManager(get_settings())
    assets = build_assets(database)
    csl = export_csl_json(assets, destination / "research-assets.csl.json")
    bib = export_bibtex(assets, destination / "research-assets.bib")
    print(f"papers={len(assets)}")
    print(f"csl_json={csl}")
    print(f"bibtex={bib}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
