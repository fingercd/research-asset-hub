from __future__ import annotations

from pathlib import Path

import pytest

from apps.asset_gateway.registry import SourceRegistry
from packages.managed_sources.service import (
    data_directory,
    register_managed_source,
)


def test_default_test_library_ignores_saved_user_managed_sources(
    isolated_user_library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_library = tmp_path / "saved-user-library"
    register_managed_source(
        saved_library,
        {
            "id": "user-only-source",
            "domain": "user-only-domain",
            "adapter": "generic_manifest",
            "asset_roots": {"pdf": str(saved_library / "library" / "papers")},
        },
        {"items": []},
    )
    local = tmp_path / "user-settings"
    preference = local / "ResearchAssetHub" / "data-directory.txt"
    preference.parent.mkdir(parents=True)
    preference.write_text(str(saved_library), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    config = tmp_path / "sources.yaml"
    config.write_text("sources: []\n", encoding="utf-8")

    assert data_directory() == isolated_user_library.resolve()
    assert SourceRegistry.load(config).describe() == []


def test_library_selection_tests_can_override_default_isolation(
    isolated_user_library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "explicit-fixture-library"
    with monkeypatch.context() as selection:
        selection.setenv("HUB_DATA_DIR", str(selected))
        assert data_directory() == selected.resolve()
    assert data_directory() == isolated_user_library.resolve()
