"""Build the four Meilisearch keyword indexes from PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.indexing import IndexBuilder, RemoteIndexSuite
from packages.indexing.builder import KeywordBuildReport

DEFAULT_DATABASE_URL = (
    "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url", default=os.getenv("HUB_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    parser.add_argument(
        "--meilisearch-url",
        default=os.getenv("HUB_MEILISEARCH_URL", "http://127.0.0.1:7700"),
    )
    parser.add_argument(
        "--meilisearch-api-key",
        default=os.getenv("HUB_MEILISEARCH_API_KEY", "research-hub-local-meili-key"),
    )
    parser.add_argument("--version", default="v1")
    parser.add_argument("--write-batch-size", type=int, default=256)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("work/validation/index-build.json"),
    )
    return parser.parse_args()


def _sqlalchemy_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


def main() -> None:
    args = _arguments()
    suite = RemoteIndexSuite.connect(
        meilisearch_url=args.meilisearch_url,
        meilisearch_api_key=args.meilisearch_api_key,
    )
    engine = create_engine(_sqlalchemy_url(args.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            report: KeywordBuildReport = IndexBuilder(
                suite,
                version=args.version,
                write_batch_size=args.write_batch_size,
            ).rebuild_keywords(session)
            session.commit()
        rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    finally:
        engine.dispose()
        suite.close()


if __name__ == "__main__":
    main()
