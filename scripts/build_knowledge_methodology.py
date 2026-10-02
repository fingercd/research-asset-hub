"""Verify every methodology citation before publishing the rebuildable card cache."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from apps.api.code_browser import _safe_child
from apps.api.config import PROJECT_ROOT, ApiSettings
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json
from packages.knowledge.documents import page_evidence_id, text_hash


def verify_card(
    card: dict[str, Any],
    root: Path,
    registry: SourceRegistry,
    repository_map: dict[str, dict[str, Any]],
) -> int:
    if not card["asset_ids"] or not card["evidence"]:
        raise ValueError(f"{card['id']}: source evidence is required.")
    for evidence in card["evidence"]:
        if card["kind"] == "code":
            repo = repository_map[evidence["representation_id"]]
            if repo["asset_id"] != evidence["asset_id"]:
                raise ValueError("Code evidence belongs to another repository.")
            directory = registry.resolve(repo["source_id"], repo["root_key"], repo["path_ref"])
            path = _safe_child(directory, evidence["path"])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != evidence["source_hash"]:
                raise ValueError(f"{card['id']}: code fingerprint mismatch.")
            lines = raw.decode("utf-8-sig").splitlines()
            quote = "\n".join(lines[evidence["line_start"] - 1 : evidence["line_end"]])
            if quote != evidence["quote"]:
                raise ValueError(f"{card['id']}: code quote or line range mismatch.")
        else:
            owner = str(UUID(evidence["asset_id"]))
            document = json.loads(
                (root / "v1" / owner / "document.json").read_text(encoding="utf-8")
            )
            if (
                document["source_hash"] != evidence["source_hash"]
                or document["representation_id"] != evidence["representation_id"]
            ):
                raise ValueError(f"{card['id']}: paper evidence version mismatch.")
            page = next(p for p in document["pages"] if p["page"] == evidence["page"])
            if evidence["quote"] not in page["text"]:
                raise ValueError(f"{card['id']}: quote absent from the specified original page.")
            evidence["evidence_id"] = page_evidence_id(
                document["representation_id"],
                document["source_hash"],
                page["page"],
                text_hash(page["text"]),
            )
    return len(card["evidence"])


def main() -> int:
    root = PROJECT_ROOT / "outputs/knowledge-pilot"
    pilot = json.loads((root / "pilot.json").read_text(encoding="utf-8"))
    repository_map = {r["representation_id"]: r for p in pilot["papers"] for r in p["repositories"]}
    registry = SourceRegistry.load(ApiSettings().sources_config_path)
    cards = []
    evidence_count = 0
    for path in sorted((PROJECT_ROOT / "config/knowledge-methodology").glob("*-pilot.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        if source["schema_version"] != 1:
            raise ValueError("Unsupported methodology source schema.")
        for card in source["cards"]:
            evidence_count += verify_card(card, root, registry, repository_map)
            cards.append(card)
    if len({c["id"] for c in cards}) != len(cards):
        raise ValueError("Methodology IDs must be unique.")
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "cards": cards,
        "verification": {
            "cards": len(cards),
            "evidence": evidence_count,
            "failures": 0,
            "counts": dict(Counter(c["kind"] for c in cards)),
            "source_review": "agent-source-review; no human review or execution implied",
        },
    }
    atomic_write_json(root / "methodology.json", result)
    print(json.dumps(result["verification"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
