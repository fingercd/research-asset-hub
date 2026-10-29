"""Preview or apply auditable paper-to-repository relation reconciliation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlalchemy.orm import Session

from apps.api.config import ApiSettings, get_settings
from apps.api.database import create_database_engine
from packages.domain.enums import ConfirmationStatus
from packages.ingestion.repository_relations import reconcile_repository_relations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source")
    parser.add_argument(
        "--confirmation-status",
        choices=("candidate", "confirmed"),
        default="candidate",
    )
    parser.add_argument("--database-url")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = (
        ApiSettings(database_url=arguments.database_url)
        if arguments.database_url
        else get_settings()
    )
    engine = create_database_engine(settings)
    try:
        with Session(engine) as session:
            report = reconcile_repository_relations(
                session,
                apply=arguments.apply,
                source_alias=arguments.source,
                confirmation_status=ConfirmationStatus(
                    arguments.confirmation_status
                ),
            )
            if arguments.apply:
                session.commit()
            else:
                session.rollback()
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
