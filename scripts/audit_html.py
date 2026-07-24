"""Audit registered HTML assets without changing sources or database state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from packages.integrations.html_audit import audit_registered_html


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Statically audit every registered HTML representation and optionally "
            "sample signed gateway sessions with Playwright."
        )
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "html-audit.json",
    )
    parser.add_argument(
        "--dynamic-samples",
        type=int,
        default=12,
        help="Number of higher-risk pages to open through signed gateway sessions.",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Skip browser checks while explicitly recording them as unverified.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    parser.add_argument(
        "--api-origin",
        default="http://127.0.0.1:8767",
        help="Loopback API origin used only to request signed open-actions.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    if arguments.limit is not None and arguments.limit < 0:
        raise SystemExit("--limit cannot be negative")
    if arguments.dynamic_samples < 0:
        raise SystemExit("--dynamic-samples cannot be negative")
    settings = get_settings()
    database = DatabaseManager(settings)
    registry = SourceRegistry.load(settings.sources_config_path)
    try:
        report = audit_registered_html(
            database,
            registry,
            limit=arguments.limit,
            dynamic_sample_size=(
                0 if arguments.static_only else arguments.dynamic_samples
            ),
            api_origin=arguments.api_origin,
            timeout_seconds=arguments.timeout_seconds,
        )
        rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
