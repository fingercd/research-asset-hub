"""Public defaults are usable without the maintainer's private research corpus."""

from pathlib import Path

import yaml

from packages.cli.config import load_sources
from packages.domain_portals import load_portal_index
from packages.ingestion.reconciliation import load_approved_weak_merges
from scripts.evaluate_keyword_knowledge import questions_for_split

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_public_configuration_starts_without_registered_assets(tmp_path: Path) -> None:
    config = PROJECT_ROOT / "config"
    assert load_sources(config / "sources.yaml", data_dir=tmp_path) == []
    taxonomy = yaml.safe_load((config / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert taxonomy["domains"] == []
    assert load_approved_weak_merges(config / "identity-merge-approvals.yaml") == {}


def test_empty_public_portal_index_is_valid(tmp_path: Path) -> None:
    (tmp_path / "portal-index.yaml").write_text(
        "schema_version: research-asset-hub.domain-portal-index/v2\nportals: []\n",
        encoding="utf-8",
    )
    index = load_portal_index(tmp_path)
    assert index.portals == []


def test_public_runtime_has_an_explicit_empty_merge_approval_registry() -> None:
    path = PROJECT_ROOT / "newconfig/release-defaults/identity-merge-approvals.yaml"
    assert load_approved_weak_merges(path) == {}


def test_keyword_split_selection_does_not_need_private_labels() -> None:
    dataset = {
        "questions": [
            {"id": "fixture-dev", "split": "dev", "query": "Example question"},
            {"id": "fixture-holdout", "split": "holdout", "query": "Other question"},
        ]
    }
    assert [row["id"] for row in questions_for_split(dataset, "dev")] == ["fixture-dev"]
    assert [row["id"] for row in questions_for_split(dataset, "holdout")] == ["fixture-holdout"]
