"""Retry only extraction failures whose error classes are known to be recoverable."""

from __future__ import annotations

import argparse

from apps.api.config import get_settings
from apps.api.database import DatabaseManager
from packages.ingestion.job_recovery import (
    DEFAULT_RECOVERABLE_EXTRACTION_ERRORS,
    retry_failed_extractions,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--error-type",
        action="append",
        dest="error_types",
        help="Recoverable error class to retry; may be supplied more than once.",
    )
    args = parser.parse_args()
    database = DatabaseManager(get_settings())
    with database.session() as session:
        summary = retry_failed_extractions(
            session,
            error_types=args.error_types or DEFAULT_RECOVERABLE_EXTRACTION_ERRORS,
        )
    print(summary)
    database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
