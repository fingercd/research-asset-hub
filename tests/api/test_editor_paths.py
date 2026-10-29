from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from apps.api.editor import (
    repository_components,
    repository_slug,
    resolve_editor_path,
)
from apps.api.routes_v1 import _representation_action_status
from apps.asset_gateway.registry import SourceRegistry


def _representation(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "representation_type": "repository",
        "source_id": "source-1",
        "root_key": "repository",
        "path_ref": None,
        "external_url": None,
        "materialization_kind": "directory",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registry(tmp_path: Path) -> SourceRegistry:
    config = tmp_path / "sources.yaml"
    config.write_text(
        (
            "schema_version: 1\n"
            "workspace_root: .\n"
            "sources:\n"
            "  - id: source-1\n"
            "    root: .\n"
            "    asset_roots:\n"
            "      repository: repos\n"
        ),
        encoding="utf-8",
    )
    return SourceRegistry.load(config)


def test_resolve_directory_representation(tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "autogen"
    repo.mkdir(parents=True)
    registry = _registry(tmp_path)
    resolution = resolve_editor_path(
        registry,
        _representation(path_ref="autogen", materialization_kind="directory"),
    )
    assert resolution.available is True
    assert resolution.path == repo.resolve().as_posix()
    assert resolution.uri == f"vscode://file/{repo.resolve().as_posix()}"


def test_resolve_readme_parent_directory(tmp_path: Path) -> None:
    readme = tmp_path / "repos" / "autogen" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text("# autogen\n", encoding="utf-8")
    registry = _registry(tmp_path)
    resolution = resolve_editor_path(
        registry,
        _representation(
            path_ref="autogen/README.md",
            materialization_kind="file",
        ),
    )
    assert resolution.available is True
    assert resolution.path == readme.parent.resolve().as_posix()


def test_resolve_url_slug_against_existing_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "Dao-AILab--flash-attention"
    repo.mkdir(parents=True)
    registry = _registry(tmp_path)
    resolution = resolve_editor_path(
        registry,
        _representation(
            path_ref=None,
            materialization_kind="url",
            external_url="https://github.com/Dao-AILab/flash-attention",
        ),
    )
    assert resolution.available is True
    assert resolution.path == repo.resolve().as_posix()


def test_local_repository_overrides_network_required_status(tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "OpenRobotLab-StreamVLN"
    repo.mkdir(parents=True)
    registry = _registry(tmp_path)
    representation = _representation(
        path_ref=None,
        materialization_kind="url",
        external_url="https://github.com/OpenRobotLab/StreamVLN",
        availability="available",
        network_required=True,
    )

    assert (
        _representation_action_status(
            representation,
            source_registry=registry,
            source_alias="source-1",
        )
        == "available"
    )


def test_source_alias_parameter_is_used_when_database_id_differs(tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "autogen"
    repo.mkdir(parents=True)
    registry = _registry(tmp_path)
    representation = _representation(
        source_id="00000000-0000-0000-0000-000000000001",
        path_ref="autogen",
        materialization_kind="directory",
    )
    resolution = resolve_editor_path(
        registry,
        representation,
        source_alias="source-1",
    )
    assert resolution.available is True
    assert resolution.path == repo.resolve().as_posix()


def test_url_slug_is_unavailable_without_local_directory(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    resolution = resolve_editor_path(
        registry,
        _representation(
            path_ref=None,
            materialization_kind="url",
            external_url="https://github.com/OpenRobotLab/StreamVLN",
        ),
    )
    assert resolution.available is False
    assert resolution.reason == "源码未下载/未开源"


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "repos").mkdir(parents=True)
    registry = _registry(tmp_path)
    resolution = resolve_editor_path(
        registry,
        _representation(path_ref="../outside", materialization_kind="directory"),
    )
    assert resolution.available is False
    assert resolution.reason == "源码路径无效"


def test_non_code_representation_is_not_an_editor_target(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    resolution = resolve_editor_path(
        registry,
        _representation(representation_type="pdf"),
    )
    assert resolution.available is False
    assert resolution.reason == "该入口不是代码项目。"


def test_repository_components_and_slug() -> None:
    assert repository_components("https://github.com/OpenRobotLab/StreamVLN") == (
        "OpenRobotLab",
        "StreamVLN",
    )
    assert repository_slug("https://github.com/Dao-AILab/flash-attention") == (
        "Dao-AILab-flash-attention"
    )
    assert repository_components("https://huggingface.co/datasets/gaia-benchmark/GAIA") == (
        "gaia-benchmark",
        "GAIA",
    )
