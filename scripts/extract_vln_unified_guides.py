"""Extract all inventoried VLN PDFs into a cache for unified guide authoring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.config import PROJECT_ROOT, get_settings
from apps.asset_gateway.registry import SourceRegistry
from packages.vln_guides import extract_inventory


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=PROJECT_ROOT / "work" / "mineru" / "vln-20260904" / "inventory.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "work" / "mineru" / "vln-20260904" / "extracted",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    if arguments.workers < 1 or arguments.workers > 16:
        raise SystemExit("--workers must be between 1 and 16")
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be positive")
    inventory = json.loads(arguments.inventory.read_text(encoding="utf-8"))
    registry = SourceRegistry.load(get_settings().sources_config_path)
    report = extract_inventory(
        inventory,
        registry=registry,
        output_root=arguments.output_root,
        workers=arguments.workers,
        force=arguments.force,
        limit=arguments.limit,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "requested": report["requested"],
                "completed": report["completed"],
                "status_counts": report["status_counts"],
                "totals": report["totals"],
                "errors": report["errors"],
                "report": str(arguments.output_root / "batch-report.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
