"""Reconcile legacy portal UUIDs through reviewed title/domain pairs."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from packages.domain.exporters.common import atomic_write_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PORTAL_ROOT = PROJECT_ROOT / "config" / "domain-portals" / "v2"
CATALOG = PROJECT_ROOT / "outputs" / "open-catalog" / "catalog.jsonl"
TARGET_SLUGS = (
    "video-llm-compression",
    "embodied-ai-compression",
    "navigation-compression",
)


def _normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _collect_pairs(value: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(value, dict):
        asset_id = value.get("asset_id")
        title = value.get("title")
        if isinstance(asset_id, str) and isinstance(title, str):
            result.append((asset_id, title))
        for child in value.values():
            result.extend(_collect_pairs(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_collect_pairs(child))
    return result


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace(child, replacements) for child in value]
    if isinstance(value, dict):
        return {key: _replace(child, replacements) for key, child in value.items()}
    return value


def main() -> None:
    catalog: dict[tuple[str, str], set[str]] = defaultdict(set)
    catalog_ids: set[str] = set()
    with CATALOG.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            asset = record["asset"]
            asset_id = str(asset["asset_id"])
            catalog_ids.add(asset_id)
            for slug in asset.get("domains") or []:
                for title in (asset.get("title"), asset.get("title_zh")):
                    if isinstance(title, str) and title.strip():
                        catalog[(str(slug), _normalized_title(title))].add(asset_id)

    replacements: dict[str, str] = {}
    documents: dict[Path, dict[str, Any]] = {}
    for slug in TARGET_SLUGS:
        directory = PORTAL_ROOT / slug
        for path in sorted(directory.glob("*.yaml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError(f"portal YAML must be an object: {path}")
            documents[path] = document
            for asset_id, title in _collect_pairs(document):
                if asset_id in catalog_ids:
                    continue
                candidates = catalog.get((slug, _normalized_title(title)), set())
                if len(candidates) != 1:
                    raise ValueError(
                        f"{slug}: {asset_id} title {title!r} has {len(candidates)} candidates"
                    )
                canonical = next(iter(candidates))
                previous = replacements.get(asset_id)
                if previous is not None and previous != canonical:
                    raise ValueError(f"conflicting replacement for {asset_id}")
                replacements[asset_id] = canonical

    changed: list[str] = []
    for path, document in documents.items():
        updated = _replace(document, replacements)
        if updated != document:
            atomic_write_text(
                path,
                yaml.safe_dump(
                    updated,
                    allow_unicode=True,
                    sort_keys=False,
                    width=100,
                ),
            )
            changed.append(path.relative_to(PROJECT_ROOT).as_posix())
    print(
        json.dumps(
            {
                "status": "updated" if changed else "unchanged",
                "replacements": len(replacements),
                "changed_files": changed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
