"""Audit all generated non-UAV VLN unified guides."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from apps.api.config import PROJECT_ROOT
from packages.vln_guides import audit_guides

WORK_ROOT = PROJECT_ROOT / "work" / "mineru" / "vln-20260904"
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "个人新增资料" / "2026-09-12-vln-guides-v1"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=WORK_ROOT / "inventory.json")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--extraction-root", type=Path, default=WORK_ROOT / "extracted")
    parser.add_argument(
        "--exclusion-policy",
        type=Path,
        default=PROJECT_ROOT / "config" / "domain-scope-exclusions.yaml",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=WORK_ROOT / "guide-audit.json",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    inventory = json.loads(arguments.inventory.read_text(encoding="utf-8"))
    policy = yaml.safe_load(arguments.exclusion_policy.read_text(encoding="utf-8")) or {}
    excluded = {
        str(asset_id)
        for rule in policy.get("rules", [])
        for asset_id in rule.get("asset_ids", [])
    }
    report = audit_guides(
        inventory,
        output_root=arguments.output_root,
        report_path=arguments.report,
        excluded_asset_ids=excluded,
        extraction_root=arguments.extraction_root,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "expected": report["expected"],
                "found": report["found"],
                "passed": report["passed"],
                "failed": report["failed"],
                "missing": len(report["missing"]),
                "stale": len(report["stale"]),
                "excluded_present": len(report["excluded_asset_ids_present"]),
                "report": str(arguments.report),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
