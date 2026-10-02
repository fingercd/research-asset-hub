from __future__ import annotations

from pathlib import Path

from apps.asset_gateway.registry import SourceRegistry


def _write_sources(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "workspace_root: ..\n"
        "sources:\n"
        "  - id: demo\n"
        "    root: library\n"
        "    asset_roots:\n"
        "      pdf: papers\n",
        encoding="utf-8",
    )


def test_load_keeps_relative_workspace_behavior_without_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "installed" / "app" / "config" / "sources.yaml"
    _write_sources(config_path)
    monkeypatch.delenv("HUB_HOST_WORKSPACE_ROOT", raising=False)

    registry = SourceRegistry.load(config_path)

    expected_workspace = config_path.parent.parent.resolve()
    assert registry.root_for("demo", "source") == expected_workspace / "library"
    assert registry.root_for("demo", "pdf") == expected_workspace / "library" / "papers"


def test_load_uses_host_workspace_override_for_installed_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "installed" / "app" / "config" / "sources.yaml"
    _write_sources(config_path)
    host_workspace = tmp_path / "host-workspace"
    host_workspace.mkdir()
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", str(host_workspace))

    registry = SourceRegistry.load(config_path)

    assert registry.root_for("demo", "source") == host_workspace.resolve() / "library"
    assert registry.root_for("demo", "pdf") == host_workspace.resolve() / "library" / "papers"


def test_root_status_reports_configured_existing_and_missing_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "installed" / "app" / "config" / "sources.yaml"
    _write_sources(config_path)
    host_workspace = tmp_path / "host-workspace"
    (host_workspace / "library").mkdir(parents=True)
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", str(host_workspace))

    status = SourceRegistry.load(config_path).root_status()

    assert status["workspace_root"] == str(host_workspace.resolve())
    assert status["configured"] == 2
    assert status["existing"] == 1
    assert status["missing"] == 1
    assert status["missing_roots"] == [
        {
            "source_id": "demo",
            "root_key": "pdf",
            "path": str((host_workspace / "library" / "papers").resolve()),
        }
    ]


def test_parent_directory_override_exposes_extra_level_regression(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "核心领域" / "research-asset-hub"
    config_path = project_root / "config" / "sources.yaml"
    _write_sources(config_path)
    (project_root / "library" / "papers").mkdir(parents=True)
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", str(project_root.parent))

    status = SourceRegistry.load(config_path).root_status()

    assert status["configured"] == 2
    assert status["existing"] == 0
    assert status["missing"] == 2
