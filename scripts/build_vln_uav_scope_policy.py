"""Build the reviewable VLN UAV exclusion policy from the audited asset set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from apps.api.config import PROJECT_ROOT

TITLE_PATTERNS = [
    r"\bUAVs?\b",
    r"\bAerial\b",
    r"\bDrones?\b",
    r"\bQuadrotor\b",
    r"\bFlights?\b",
    r"\b(?:Dream|Recover|World|Dyn|Auto|On)Fly\b",
    r"\bFly0\b",
    r"\bAero(?:Act|DPO)\b",
    r"\bAirForesight\b",
    r"\bImagineUAV\b",
    r"\bThink Like a Pilot\b",
]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "vln-uav-scope-audit.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "config" / "domain-scope-exclusions.yaml",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    audit = json.loads(arguments.audit.read_text(encoding="utf-8"))
    if audit.get("status") != "passed" or audit.get("excluded_paper_count") != 31:
        raise SystemExit("expected a passed audit with exactly 31 UAV-focused papers")
    paper_ids = [str(item["asset_id"]) for item in audit["papers"]]
    repository_ids = [
        str(item["asset_id"])
        for item in audit["repositories"]
        if item.get("currently_in_vln") and item.get("exclude_from_vln")
    ]
    document: dict[str, Any] = {
        "schema": "research-asset-hub.domain-scope-exclusions/v1",
        "rules": [
            {
                "id": "exclude-uav-focused-navigation-from-vln",
                "domain": "vision-language-navigation",
                "reason": (
                    "User scope excludes UAV, aerial-robot, drone, and quadrotor "
                    "navigation research from the VLN domain."
                ),
                "title_patterns": TITLE_PATTERNS,
                "asset_ids": sorted({*paper_ids, *repository_ids}),
                "reviewed_counts": {
                    "papers": len(paper_ids),
                    "repositories": len(repository_ids),
                },
                "reviewed_exceptions": [
                    "Embodied4C: Measuring What Matters for Embodied Vision-Language Navigation",
                    "Embodied Navigation Foundation Model",
                ],
            }
        ],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "papers": len(paper_ids),
                "repositories": len(repository_ids),
                "asset_ids": len(document["rules"][0]["asset_ids"]),
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
