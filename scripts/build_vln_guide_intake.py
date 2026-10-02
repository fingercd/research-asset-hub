"""Build a reviewed 109-paper HTML sidecar; original papers remain in place."""

from __future__ import annotations

import hashlib
import json

from apps.api.config import PROJECT_ROOT
from packages.domain.exporters.common import atomic_write_json
from packages.vln_guides.audit import audit_guides

SOURCE_ID = "vln-unified-guides-non-uav-20260912-v1"
WORK_ROOT = PROJECT_ROOT / "work" / "mineru" / "vln-20260904"
GUIDE_ROOT = PROJECT_ROOT.parent / "个人新增资料" / "2026-09-12-vln-guides-v1"


def main() -> int:
    inventory = json.loads((WORK_ROOT / "inventory.json").read_text(encoding="utf-8"))
    if inventory["status"] != "passed" or inventory["paper_count"] != 109:
        raise ValueError("Expected the reviewed 109-paper inventory")
    if inventory["scope_exclusions"]["status"] != "passed":
        raise ValueError("UAV scope check failed")
    quality = audit_guides(
        inventory,
        output_root=GUIDE_ROOT,
        report_path=WORK_ROOT / "guide-audit.json",
        extraction_root=WORK_ROOT / "extracted",
    )
    if quality["status"] != "passed":
        raise ValueError(f"Guide quality gate failed for {quality['failed']} papers")
    items = []
    for row in inventory["items"]:
        asset_id = row["asset_id"]
        relative_path = f"papers/{asset_id}/guide.html"
        content = json.loads(
            (GUIDE_ROOT / "papers" / asset_id / "content.json").read_text(encoding="utf-8")
        )
        identifiers = {
            item["scheme"]: item["value"]
            for item in row["identifiers"]
            if item["is_identity"] and item["scheme"] in {"doi", "arxiv"}
        }
        items.append(
            {
                "id": f"guide-{asset_id}",
                **{
                    key: row[key]
                    for key in ("title", "title_zh", "authors", "year", "venue", "source_urls")
                },
                "doi": identifiers.get("doi"),
                "arxiv_id": identifiers.get("arxiv"),
                "summary": "\n\n".join(content["one_minute"]),
                "attributes": {
                    "unified_guide": {
                        "version": "v1",
                        "source_asset_id": asset_id,
                        "review": content["review"],
                        "provenance": content["provenance"],
                    },
                },
                "representations": {
                    "guide": {
                        "path": relative_path,
                        "sha256": hashlib.sha256(
                            (GUIDE_ROOT / relative_path).read_bytes()
                        ).hexdigest(),
                    }
                },
            }
        )
    document = {
        "schema_version": 1,
        "source_id": SOURCE_ID,
        "asset_id_set_sha256": inventory["asset_id_set_sha256"],
        "items": items,
    }
    destination = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"
    if destination.exists():
        if json.loads(destination.read_text(encoding="utf-8")) != document:
            raise ValueError("Manifest already exists with different content; use a new version")
    else:
        atomic_write_json(destination, document)
    print(json.dumps({"status": "passed", "source_id": SOURCE_ID, "records": len(items)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
