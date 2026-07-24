"""Export the PostgreSQL source of truth as an open, binary-free catalog."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.api.config import PROJECT_ROOT, ApiSettings, get_settings
from apps.api.database import DatabaseManager
from packages.domain.exporters.common import atomic_write_json
from packages.exporting import export_operational_catalog

DEFAULT_DESTINATION = PROJECT_ROOT / "outputs" / "open-catalog"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export canonical metadata, relations and provenance without copying source binaries."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DESTINATION,
        help="Catalog destination (default: outputs/open-catalog).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help=(
            "Optional additional path for the manifest. The catalog always contains manifest.json."
        ),
    )
    return parser


def run_export(
    settings: ApiSettings,
    destination: Path,
    *,
    database: DatabaseManager | None = None,
) -> dict[str, Any]:
    """Run one catalog export against the configured operational database."""

    manager = database or DatabaseManager(settings)
    with manager.session() as session:
        manifest = export_operational_catalog(session, destination)
    if manifest.get("source_binaries_copied") != 0:
        raise RuntimeError("Catalog exporter reported a source binary copy.")
    return manifest


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: ApiSettings | None = None,
    database: DatabaseManager | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    active_settings = settings or get_settings()
    destination = arguments.output.expanduser().resolve()
    manifest = run_export(
        active_settings,
        destination,
        database=database,
    )
    if arguments.report is not None:
        atomic_write_json(arguments.report.expanduser().resolve(), manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
