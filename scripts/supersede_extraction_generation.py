"""Cancel obsolete queued extraction jobs before seeding a parser generation."""

from __future__ import annotations

import argparse

from apps.api.config import get_settings
from apps.api.database import DatabaseManager
from packages.ingestion.job_recovery import supersede_extraction_generation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-generation", required=True)
    parser.add_argument("--to-generation", required=True)
    args = parser.parse_args()
    database = DatabaseManager(get_settings())
    try:
        with database.session() as session:
            summary = supersede_extraction_generation(
                session,
                previous_generation=args.from_generation,
                successor_generation=args.to_generation,
            )
        print(summary)
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
