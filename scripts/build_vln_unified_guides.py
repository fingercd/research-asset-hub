"""Build the VLN authoring queue and render self-contained local HTML guides."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.config import PROJECT_ROOT
from packages.vln_guides import build_authoring_queue, render_inventory

WORK_ROOT = PROJECT_ROOT / "work" / "mineru" / "vln-20260904"
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "个人新增资料" / "2026-09-12-vln-guides-v1"
DEFAULT_AUTHORED = PROJECT_ROOT / "config" / "vln-guides" / "v1" / "content"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=WORK_ROOT / "inventory.json")
    parser.add_argument(
        "--extraction-root",
        type=Path,
        default=WORK_ROOT / "extracted",
    )
    parser.add_argument(
        "--authored-root",
        type=Path,
        default=DEFAULT_AUTHORED,
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=WORK_ROOT / "authoring-queue.json",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--queue-only", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    if arguments.queue_only and arguments.render_only:
        raise SystemExit("--queue-only and --render-only are mutually exclusive")
    inventory = json.loads(arguments.inventory.read_text(encoding="utf-8"))
    result: dict[str, object] = {}
    if not arguments.render_only:
        queue = build_authoring_queue(
            inventory,
            extraction_root=arguments.extraction_root,
            output_path=arguments.queue,
            authored_root=arguments.authored_root,
        )
        result["queue"] = {
            "status": queue["status"],
            "requires_authored_content": queue["requires_authored_content"],
            "path": str(arguments.queue),
        }
    if not arguments.queue_only:
        render = render_inventory(
            inventory,
            extraction_root=arguments.extraction_root,
            authored_root=arguments.authored_root,
            output_root=arguments.output_root,
        )
        result["render"] = {
            "status": render["status"],
            "rendered": render["rendered"],
            "content_sources": render["content_sources"],
            "path": str(arguments.output_root / "render-report.json"),
        }
    print(json.dumps(result, ensure_ascii=False))
    render_result = result.get("render")
    if isinstance(render_result, dict) and render_result.get("status") == "draft":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
