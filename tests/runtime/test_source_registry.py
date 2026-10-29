from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from apps.asset_gateway.registry import SourceConfigurationError, SourceRegistry


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


def test_root_status_reports_registration_without_probing_historical_roots(
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
    assert status["verification"] == "registration_only"
    assert status["source_files_verified"] is False
    assert "missing" not in status and "existing" not in status

    registry = SourceRegistry({("historical", "pdf"): host_workspace / "removed"})
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: pytest.fail("root was probed"))
    assert registry.root_status()["configured"] == 1


def test_parent_directory_override_resolves_declared_roots_without_availability_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "核心领域" / "research-asset-hub"
    config_path = project_root / "config" / "sources.yaml"
    _write_sources(config_path)
    (project_root / "library" / "papers").mkdir(parents=True)
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", str(project_root.parent))

    registry = SourceRegistry.load(config_path)
    status = registry.root_status()

    assert status["configured"] == 2
    assert registry.root_for("demo", "source") == project_root.parent / "library"
    assert status["source_files_verified"] is False


def test_shared_registry_sees_new_roots_and_changed_workspace_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_HOST_WORKSPACE_ROOT", raising=False)
    config_path = tmp_path / "config" / "sources.yaml"
    _write_sources(config_path)
    registry = SourceRegistry.load(config_path)
    consumer = registry
    before = registry.configuration_status()["configuration_sha256"]
    config_path.write_text(
        "workspace_root: ../new-workspace\nsources:\n"
        "  - id: new-source\n    root: intake\n    read_only: true\n"
        "    asset_roots:\n      pdf: papers\n",
        encoding="utf-8",
    )
    assert consumer is registry
    assert (
        consumer.resolve("new-source", "pdf", "paper.pdf")
        == tmp_path / "new-workspace" / "intake" / "papers" / "paper.pdf"
    )
    assert registry.configuration_status()["configuration_sha256"] != before
    assert registry.refresh_if_changed() is False
    assert {item.source_id for item in registry.describe()} == {"new-source"}
    assert registry.root_status()["workspace_root"] == str(tmp_path / "new-workspace")


@pytest.mark.parametrize(
    "replacement",
    [
        "sources: [",
        "sources: [null]",
        "sources: [{id: demo, root: one}, {id: demo, root: two}]",
        "sources: [{id: demo, root: one, asset_roots: null}]",
        "sources: [{id: demo, root: one, read_only: false}]",
    ],
)
def test_bad_refresh_preserves_complete_snapshot_and_fails_clearly(tmp_path, replacement):
    config_path = tmp_path / "config" / "sources.yaml"
    _write_sources(config_path)
    registry = SourceRegistry.load(config_path)
    snapshot = registry._snapshot
    config_path.write_text(replacement, encoding="utf-8")
    with pytest.raises(SourceConfigurationError, match="previous complete registry"):
        registry.root_for("new-source", "pdf")
    assert registry._snapshot is snapshot
    _write_sources(config_path)
    assert registry.root_for("demo", "pdf") == tmp_path / "library" / "papers"


def test_refresh_exclusions_atomically_and_reject_invalid_policy(tmp_path):
    config_path = tmp_path / "config" / "sources.yaml"
    _write_sources(config_path)
    registry = SourceRegistry.load(config_path)
    policy_path = config_path.parent / "operational-path-exclusions.yaml"
    policy_path.write_text("schema_version: 1\nenabled: true\nroots: []\n", encoding="utf-8")
    assert registry.refresh_if_changed()
    assert registry.operational_exclusions.enabled
    snapshot = registry._snapshot
    policy_path.write_text("schema_version: wrong\nenabled: true\n", encoding="utf-8")
    with pytest.raises(SourceConfigurationError):
        registry.describe()
    assert registry._snapshot is snapshot
    policy_path.unlink()
    assert registry.refresh_if_changed()
    assert not registry.operational_exclusions.enabled


def test_static_registry_remains_usable_and_diagnostics_hide_host_paths(tmp_path):
    registry = SourceRegistry({("demo", "pdf"): tmp_path})
    assert not registry.refresh_if_changed()
    assert registry.resolve("demo", "pdf", "paper.pdf") == tmp_path / "paper.pdf"
    diagnostic = registry.configuration_status()
    assert diagnostic["hot_refresh_enabled"] is False
    assert diagnostic["loaded_configuration_current"] is None
    assert str(tmp_path) not in str(diagnostic)


def test_concurrent_consumers_reload_one_complete_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_HOST_WORKSPACE_ROOT", raising=False)
    config_path = tmp_path / "config" / "sources.yaml"
    _write_sources(config_path)
    registry = SourceRegistry.load(config_path)
    reads = []
    original_reader = registry._read_snapshot

    def counted_reader(path, signature):
        reads.append(signature)
        return original_reader(path, signature)

    monkeypatch.setattr(registry, "_read_snapshot", counted_reader)
    config_path.write_text(
        "workspace_root: ..\nsources:\n  - id: new-source\n    root: new-library\n"
        "    asset_roots:\n      pdf: papers\n",
        encoding="utf-8",
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(lambda _: registry.root_for("new-source", "pdf"), range(32)))
    assert all(path == tmp_path / "new-library" / "papers" for path in paths)
    assert len(reads) == 1


def test_installed_configuration_does_not_claim_repository_copy_is_synced(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_HOST_WORKSPACE_ROOT", raising=False)
    installed = tmp_path / "installed" / "sources.yaml"
    _write_sources(installed)
    registry = SourceRegistry.load(installed)
    before = registry.configuration_status()
    repository_copy = tmp_path / "repository" / "sources.yaml"
    _write_sources(repository_copy)
    repository_copy.write_text("sources: [{id: new, root: library}]", encoding="utf-8")
    assert registry.config_path == installed.resolve()
    assert registry.refresh_if_changed() is False
    assert registry.configuration_status() == before
    assert before["repository_copy_consistency"] == "not_verified"
    assert str(installed.resolve()) not in str(before)
