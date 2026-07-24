"""Generate the privacy-safe v1 release report from PostgreSQL and validation artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from packages.backup.release_report import (
    ReleaseReportInputs,
    build_release_report,
    collect_database_snapshot,
    load_json_report,
    write_release_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VALIDATION = PROJECT_ROOT / "work" / "validation"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "release"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="Operational database URL; defaults to HUB_DATABASE_URL/local settings.",
    )
    parser.add_argument("--validation-dir", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--work-report",
        type=Path,
        default=DEFAULT_VALIDATION / "release-report.json",
    )
    parser.add_argument(
        "--restore-report",
        type=Path,
        help="Explicit isolated restore report; otherwise the newest report is used.",
    )
    return parser.parse_args()


def _newest(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime_ns) if existing else None


def _discover_reports(
    validation_directory: Path,
    *,
    restore_report: Path | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    production_html = _newest(
        list(validation_directory.glob("html-audit-production*.json"))
    )
    bge_index_build = _newest(
        list(validation_directory.glob("index-build-bge*.json"))
    )
    fixed: dict[str, Path | None] = {
        "inventory_current": validation_directory / "baseline-inventory.json",
        "inventory_baseline": (
            validation_directory / "baseline-inventory-before-dry-run.json"
        ),
        "formal_import": validation_directory / "ingest-formal.json",
        "source_integrity": validation_directory / "source-integrity.json",
        "html_audit": (
            production_html
            or validation_directory / "html-audit.json"
        ),
        "recovery_fixture": _newest(
            list(validation_directory.glob("recovery-fixture-*verify*.json"))
        ),
        "search_evaluation": validation_directory / "search-evaluation.json",
        "rag_evaluation": validation_directory / "rag-evaluation.json",
        "index_build": (
            bge_index_build
            or _newest(list(validation_directory.glob("index-build*.json")))
        ),
        "restore_rehearsal": (
            restore_report
            or _newest(
                list(
                    (PROJECT_ROOT / "work" / "restore-tests").glob(
                        "*/report.json"
                    )
                )
            )
        ),
    }
    reports: dict[str, Any] = {}
    discovered: dict[str, str] = {}
    for name, path in fixed.items():
        if path is None or not path.is_file():
            continue
        reports[name] = load_json_report(path)
        discovered[name] = path.name
    return reports, discovered


def main() -> int:
    arguments = _arguments()
    settings = (
        ApiSettings(database_url=arguments.database_url)
        if arguments.database_url
        else ApiSettings()
    )
    database = DatabaseManager(settings)
    try:
        with database.session() as session:
            snapshot = collect_database_snapshot(session)
        reports, discovered = _discover_reports(
            arguments.validation_dir,
            restore_report=arguments.restore_report,
        )
        report = build_release_report(
            ReleaseReportInputs(database=snapshot, reports=reports)
        )
        artifacts = write_release_report(
            report,
            output_directory=arguments.output_dir,
            validation_path=arguments.work_report,
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "gate_summary": report["gate_summary"],
                    "reports_used": discovered,
                    "artifacts": artifacts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report["status"] != "fail" else 2
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "type": type(error).__name__,
                        "message": "Release report generation failed; inspect local logs.",
                    },
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
