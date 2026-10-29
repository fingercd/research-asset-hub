from __future__ import annotations

import json
from pathlib import Path

from packages.integrations import (
    ObsidianVaultAdapter,
    export_bibtex,
    export_csl_json,
    stable_citekey,
    three_way_merge,
)


def test_three_way_merge_detects_same_field_conflict() -> None:
    preview = three_way_merge(
        {"title": "Base", "year": 2024},
        {"title": "Hub", "year": 2024},
        {"title": "Obsidian", "year": 2025},
    )
    assert {conflict.field for conflict in preview.conflicts} == {"title"}
    assert preview.merged["year"] == 2025
    assert not preview.can_apply


def test_obsidian_round_trip_preserves_unicode_and_user_body(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    adapter = ObsidianVaultAdapter(vault)
    path = adapter.write(
        asset_id="018f0000-0000-7000-8000-000000000001",
        hub_revision=3,
        fields={
            "title": "世界模型综述",
            "authors": ["作者甲"],
            "tags": ["论文", "world-model"],
            "domains": ["world-model"],
            "reading_status": "reading",
            "relations": ["另一篇论文"],
        },
        note_body="这里是我的详细批注。",
    )
    document = adapter.parse(path)
    assert document.asset_id.endswith("0001")
    assert document.hub_revision == 3
    assert document.fields["title"] == "世界模型综述"
    assert document.note_body == "这里是我的详细批注。"
    assert "[[另一篇论文]]" in path.read_text(encoding="utf-8")


def test_obsidian_same_title_assets_never_share_a_path(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    adapter = ObsidianVaultAdapter(vault)
    first_id = "019f93f6-2f86-79f9-a45a-715fadf29d37"
    second_id = "019f93f6-2f86-79f9-a45a-715fadf29d38"

    first_path = adapter.write(
        asset_id=first_id,
        hub_revision=1,
        fields={"title": "同名论文"},
    )
    second_path = adapter.write(
        asset_id=second_id,
        hub_revision=1,
        fields={"title": "同名论文"},
    )

    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()
    assert first_id in first_path.name
    assert second_id in second_path.name


def test_zotero_exports_are_stable(tmp_path: Path) -> None:
    assets = [
        {
            "id": "018f00",
            "title": "A Useful Paper",
            "authors": ["Ada Lovelace"],
            "year": 2024,
            "doi": "10.1/example",
        }
    ]
    csl_path = export_csl_json(assets, tmp_path / "library.json")
    bib_path = export_bibtex(assets, tmp_path / "library.bib")
    assert json.loads(csl_path.read_text(encoding="utf-8"))[0]["DOI"] == "10.1/example"
    bibtex = bib_path.read_text(encoding="utf-8")
    assert "@article{" in bibtex
    assert "rah_asset_id" in bibtex


def test_zotero_citekeys_distinguish_uuid7_siblings() -> None:
    common = {"title": "Same Paper", "authors": ["Ada"], "year": 2026}
    first = {
        **common,
        "id": "019f93f6-2f86-79f9-a45a-715fadf29d37",
    }
    second = {
        **common,
        "id": "019f93f6-2f86-79f9-a45a-715fadf29d38",
    }
    assert stable_citekey(first) != stable_citekey(second)
