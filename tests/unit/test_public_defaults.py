"""A new library does not require curated portals or weak-merge approvals."""

from pathlib import Path

from packages.domain_portals import load_portal_index
from packages.ingestion.reconciliation import load_approved_weak_merges


def test_empty_portal_registry_is_valid(tmp_path: Path) -> None:
    (tmp_path / "portal-index.yaml").write_text(
        "schema_version: research-asset-hub.domain-portal-index/v2\nportals: []\n",
        encoding="utf-8",
    )
    assert load_portal_index(tmp_path).portals == []


def test_public_runtime_has_empty_explicit_merge_approvals() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "newconfig/release-defaults/identity-merge-approvals.yaml"
    assert load_approved_weak_merges(path) == {}
