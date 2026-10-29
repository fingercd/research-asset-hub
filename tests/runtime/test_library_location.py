"""The selected library and frozen workspace mapping are shared by all callers."""

from __future__ import annotations

from pathlib import Path

from apps.api.config import ApiSettings
from apps.asset_gateway.registry import SourceRegistry
from packages.cli.config import load_sources
from packages.managed_sources.service import data_directory


def test_saved_library_selection_and_environment_override(tmp_path: Path, monkeypatch) -> None:
    local = tmp_path / "user settings"
    preference = local / "ResearchAssetHub/data-directory.txt"
    preference.parent.mkdir(parents=True)
    selected = tmp_path / "另一磁盘 空资料库"
    preference.write_text(str(selected), encoding="utf-8-sig")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("HUB_DATA_DIR", raising=False)
    assert data_directory() == selected.resolve()
    assert ApiSettings().data_dir == selected
    override = tmp_path / "explicit library"
    monkeypatch.setenv("HUB_DATA_DIR", str(override))
    assert data_directory() == override.resolve()
    assert ApiSettings().data_dir == override


def test_user_configuration_and_workspace_hint_preserve_legacy_roots(
    tmp_path: Path, monkeypatch
) -> None:
    data = tmp_path / "资料库"
    config = data / "config"
    config.mkdir(parents=True)
    workspace = tmp_path / "原始资料工作区"
    paper_root = workspace / "paper-source"
    paper_root.mkdir(parents=True)
    (paper_root / "manifest.json").write_text('{"items": []}', encoding="utf-8")
    (config / "sources.yaml").write_text(
        "workspace_root: ..\nsources:\n  - id: legacy\n    domain: alpha\n"
        "    root: paper-source\n    manifest: manifest.json\n    read_only: true\n",
        encoding="utf-8",
    )
    (config / "taxonomy.yaml").write_text("domains: []\n", encoding="utf-8")
    (config / "workspace-root.txt").write_text(str(workspace), encoding="utf-8-sig")
    monkeypatch.setenv("HUB_DATA_DIR", str(data))
    for key in (
        "HUB_HOST_WORKSPACE_ROOT",
        "HUB_SOURCES_CONFIG_PATH",
        "HUB_TAXONOMY_CONFIG_PATH",
        "HUB_DOMAIN_PORTAL_CONFIG_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = ApiSettings()
    assert settings.sources_config_path == config / "sources.yaml"
    assert settings.domain_portal_config_path == config / "domain-portals/v2"
    registry = SourceRegistry.load(settings.sources_config_path)
    assert registry.describe()[0].path == paper_root
    assert load_sources()[0].root == paper_root
    explicit = tmp_path / "explicit-sources.yaml"
    assert ApiSettings(sources_config_path=explicit).sources_config_path == explicit
