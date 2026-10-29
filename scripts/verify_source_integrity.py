"""Verify active local source representations without mutating source files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.config import PROJECT_ROOT
from apps.asset_gateway.registry import SourceRegistry
from packages.ingestion.source_integrity import verify_source_integrity

DEFAULT_DATABASE_URL = (
    "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify active local source representations without changing them."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("HUB_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument(
        "--sources-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "sources.yaml",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "source-integrity.json",
    )
    return parser.parse_args()


def _sqlalchemy_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


def main() -> int:
    arguments = _arguments()
    registry = SourceRegistry.load(arguments.sources_config)
    engine = create_engine(_sqlalchemy_url(arguments.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            report = verify_source_integrity(session, registry)
        result = report.to_dict()
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if result["status"] == "passed" else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
