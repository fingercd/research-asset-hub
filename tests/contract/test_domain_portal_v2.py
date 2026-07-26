from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.domain_portals import load_domain_portal, load_portal_index

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "config" / "domain-portals" / "v2"
SOURCE_ROOT = PROJECT_ROOT.parent

EXPECTED_SECTIONS = [
    "scope",
    "timeline",
    "capability-method-map",
    "landmarks",
    "benchmarks",
    "reading-routes",
    "catalog",
    "frontiers",
    "evidence",
]


def _asset_ids(value: Any, *, key: str | None = None) -> set[str]:
    if isinstance(value, dict):
        result: set[str] = set()
        for child_key, child in value.items():
            result.update(_asset_ids(child, key=str(child_key)))
        return result
    if isinstance(value, list):
        if key in {"asset_ids"}:
            return {item for item in value if isinstance(item, str)}
        result = set()
        for child in value:
            result.update(_asset_ids(child, key=key))
        return result
    if key == "asset_id" and isinstance(value, str):
        return {value}
    return set()


def test_all_registered_domain_portals_are_complete_and_fresh() -> None:
    index = load_portal_index(CONFIG_ROOT)

    assert len(index.portals) == 10
    assert len({item.slug for item in index.portals}) == 10
    for item in index.portals:
        portal = load_domain_portal(CONFIG_ROOT, SOURCE_ROOT, item.slug)
        assert portal.slug == item.slug
        assert portal.review_status == "reviewed"
        assert portal.stale is False
        assert portal.stale_reasons == []
        assert [section.id for section in portal.sections] == EXPECTED_SECTIONS
        assert all(section.status == "reviewed" for section in portal.sections)


def test_every_portal_asset_reference_exists_in_its_domain() -> None:
    catalog_path = PROJECT_ROOT / "outputs" / "open-catalog" / "catalog.jsonl"
    domains_by_asset: dict[str, set[str]] = {}
    with catalog_path.open(encoding="utf-8") as handle:
        for line in handle:
            asset = json.loads(line)["asset"]
            domains_by_asset[str(asset["asset_id"])] = set(asset.get("domains") or [])

    index = load_portal_index(CONFIG_ROOT)
    for item in index.portals:
        portal = load_domain_portal(CONFIG_ROOT, SOURCE_ROOT, item.slug)
        references = _asset_ids(
            [section.model_dump(mode="json") for section in portal.sections]
        )
        assert references
        assert references <= domains_by_asset.keys()
        assert all(item.slug in domains_by_asset[asset_id] for asset_id in references)


def test_portal_loader_is_deterministic() -> None:
    first = load_domain_portal(CONFIG_ROOT, SOURCE_ROOT, "world-model")
    second = load_domain_portal(CONFIG_ROOT, SOURCE_ROOT, "world-model")

    assert first.model_dump(mode="json", by_alias=True) == second.model_dump(
        mode="json",
        by_alias=True,
    )
