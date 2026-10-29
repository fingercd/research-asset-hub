"""Build the serving Meilisearch keyword indexes from PostgreSQL.

Default maintenance builds assets and annotations. Body and code indexes are
preserved for explicit maintenance through ``--index``.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.indexing import KEYWORD_INDEX_NAMES, IndexBuilder, RemoteIndexSuite
from packages.indexing.builder import (
    DEFAULT_REFRESH_INDEX_NAMES,
    KeywordBuildReport,
    resolve_asset_scope,
)

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
    parser.add_argument("--task-flush-batches", type=int, default=16)
    parser.add_argument(
        "--asset-id",
        action="append",
        type=UUID,
        default=None,
        help="Refresh only this asset UUID; repeat for multiple assets.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Exact source alias, including its materialized repository assets.",
    )
    parser.add_argument(
        "--index",
        action="append",
        choices=KEYWORD_INDEX_NAMES,
        default=None,
        help="Maintain only this index; repeat to select several. Defaults to assets/annotations.",
    )
    parser.add_argument(
        "--skip-code",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Leave code untouched by default; --no-skip-code adds code to the default indexes.",
    )
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


def _engine_options(database_url: str) -> dict[str, object]:
    options: dict[str, object] = {"pool_pre_ping": True}
    if database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        options["isolation_level"] = "REPEATABLE READ"
    return options


def _write_report_atomic(path: Path, report: KeywordBuildReport) -> str:
    """Publish a success report only after the DB transaction committed."""

    rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(rendered + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return rendered


def main() -> None:
    args = _arguments()
    selected_indexes = tuple(
        dict.fromkeys(
            getattr(args, "index", None)
            or (*DEFAULT_REFRESH_INDEX_NAMES, *(("code",) if not args.skip_code else ()))
        )
    )
    database_url = _sqlalchemy_url(args.database_url)
    engine = create_engine(database_url, **_engine_options(database_url))
    suite = None
    try:
        with Session(engine) as session:
            asset_ids = resolve_asset_scope(
                session,
                asset_ids=getattr(args, "asset_id", None),
                source_alias=getattr(args, "source", None),
            )
            if asset_ids == ():
                raise ValueError("The requested scope contains no registered assets.")
            suite = RemoteIndexSuite.connect(
                meilisearch_url=args.meilisearch_url,
                meilisearch_api_key=args.meilisearch_api_key,
                index_names=selected_indexes,
            )
            builder = IndexBuilder(
                suite,
                version=args.version,
                write_batch_size=args.write_batch_size,
                task_flush_batch_count=args.task_flush_batches,
            )
            report: KeywordBuildReport = (
                builder.rebuild_keywords(session, index_names=selected_indexes)
                if asset_ids is None
                else builder.refresh_keywords(
                    session,
                    asset_ids=asset_ids,
                    index_names=selected_indexes,
                )
            )
            session.commit()
        rendered = _write_report_atomic(args.report, report)
        print(rendered)
    finally:
        engine.dispose()
        if suite is not None:
            suite.close()


if __name__ == "__main__":
    main()
