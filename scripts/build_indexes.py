"""Build Meilisearch/Qdrant indexes from the PostgreSQL source of truth."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.indexing import (
    HashingEmbedder,
    IndexBuilder,
    RemoteIndexSuite,
    ResilientEmbedder,
    SentenceTransformerEmbedder,
)
from packages.indexing.builder import IndexBuildReport, KeywordBuildReport

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
    parser.add_argument(
        "--qdrant-url",
        default=os.getenv("HUB_QDRANT_URL", "http://127.0.0.1:6333"),
    )
    parser.add_argument("--version", default="v1")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--write-batch-size", type=int, default=256)
    parser.add_argument("--vector-write-batch-size", type=int, default=128)
    parser.add_argument("--max-sequence-length", type=int, default=512)
    parser.add_argument(
        "--full-precision",
        action="store_true",
        help="Disable CUDA FP16 embedding inference.",
    )
    parser.add_argument(
        "--resume-vectors",
        action="store_true",
        help=(
            "Verify and reuse complete keyword/asset indexes, then rebuild "
            "the chunk vector collection."
        ),
    )
    parser.add_argument(
        "--keyword-only",
        action="store_true",
        help="Rebuild only the four Meilisearch indexes; leave vectors untouched.",
    )
    parser.add_argument(
        "--hashing-only",
        action="store_true",
        help="Explicitly skip BGE-M3 and build degraded lexical vectors.",
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


def main() -> None:
    args = _arguments()
    suite = RemoteIndexSuite.connect(
        meilisearch_url=args.meilisearch_url,
        meilisearch_api_key=args.meilisearch_api_key,
        qdrant_url=args.qdrant_url,
        dimension=1024,
        version=args.version,
    )
    engine = create_engine(_sqlalchemy_url(args.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            report: IndexBuildReport | KeywordBuildReport
            builder = IndexBuilder(
                suite,
                version=args.version,
                batch_size=args.batch_size,
                write_batch_size=args.write_batch_size,
                vector_write_batch_size=args.vector_write_batch_size,
            )
            if args.keyword_only:
                report = builder.rebuild_keywords(session)
            else:
                embedder = (
                    HashingEmbedder(dimension=1024)
                    if args.hashing_only
                    else ResilientEmbedder(
                        SentenceTransformerEmbedder(
                            model_name="BAAI/bge-m3",
                            dimension=1024,
                            batch_size=args.batch_size,
                            max_sequence_length=args.max_sequence_length,
                            use_half_precision=not args.full_precision,
                        ),
                        HashingEmbedder(dimension=1024),
                    )
                )
                builder.embedder = embedder
                report = builder.build(
                    session,
                    resume_vectors=args.resume_vectors,
                )
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
