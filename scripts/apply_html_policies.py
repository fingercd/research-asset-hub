"""Audit registered HTML and persist the resulting sandbox policies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from packages.integrations.html_audit import audit_registered_html
from packages.integrations.html_policy import (
    apply_html_policy_inferences,
    inferences_from_audit,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "html-policy-apply.json",
    )
    args = parser.parse_args()
    settings = get_settings()
    database = DatabaseManager(settings)
    try:
        audit = audit_registered_html(
            database,
            SourceRegistry.load(settings.sources_config_path),
        )
        inferences = inferences_from_audit(audit)
        with database.session() as session:
            result = apply_html_policy_inferences(session, inferences)
        output = {
            "schema": "research-asset-hub.html-policy-apply/v1",
            "audit_started_at": audit.started_at.isoformat(),
            "audit_finished_at": (
                audit.finished_at.isoformat() if audit.finished_at else None
            ),
            "inferences": len(inferences),
            **result,
        }
        rendered = json.dumps(output, ensure_ascii=False, indent=2)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
