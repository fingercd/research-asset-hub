from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGE_SCRIPT = PROJECT_ROOT / "desktop" / "scripts" / "stage-runtime.ps1"


def _root_validation() -> str:
    script = STAGE_SCRIPT.read_text(encoding="utf-8")
    return script.split("$rootValidation = @'\n", 1)[1].split("\n'@", 1)[0]


def test_stage_runtime_builds_web_before_copying_apps() -> None:
    script = STAGE_SCRIPT.read_text(encoding="utf-8")
    build = "& pnpm --filter @research-asset-hub/web build"
    copy_apps = "foreach ($item in @('apps', 'packages', 'migrations', 'config'))"

    assert build in script
    assert "if ($LASTEXITCODE -ne 0) {" in script
    assert "Web 前端构建失败，未继续复制 apps 到 stage" in script
    assert script.index(build) < script.index(copy_apps)


def _write_sources(project_root: Path) -> None:
    (project_root / "config").mkdir(parents=True)
    (project_root / "config" / "sources.yaml").write_text(
        "workspace_root: ..\n"
        "sources:\n"
        "  - id: retained-history\n"
        "    root: archive\n"
        "    asset_roots:\n"
        "      pdf: papers\n",
        encoding="utf-8",
    )


def _staged_app(tmp_path: Path, project_root: Path) -> Path:
    app_dir = tmp_path / "stage" / "app"
    shutil.copytree(
        PROJECT_ROOT / "apps" / "asset_gateway",
        app_dir / "apps" / "asset_gateway",
    )
    shutil.copytree(project_root / "config", app_dir / "config")
    return app_dir


def _validate(
    app_dir: Path, workspace_root: Path, project_root: Path
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"HUB_HOST_WORKSPACE_ROOT": str(project_root.parent / "ignored")}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _root_validation(),
            str(app_dir),
            str(workspace_root),
            str(project_root),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_root_validation_allows_correct_root_with_historical_missing_source(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "核心领域" / "research-asset-hub"
    _write_sources(project_root)
    (project_root / "archive").mkdir()
    result = _validate(_staged_app(tmp_path, project_root), project_root, project_root)

    assert result.returncode == 0, result.stderr
    assert "configured=2" in result.stdout
    assert "verification=registration_only" in result.stdout
    assert "source_files_verified=False" in result.stdout
    assert f"workspace_root={project_root}" in result.stdout
    assert "WARNING configured source roots currently missing" not in result.stdout
    assert "MISSING retained-history/pdf" not in result.stdout


def test_root_validation_rejects_parent_directory_even_when_roots_are_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "核心领域" / "research-asset-hub"
    _write_sources(project_root)
    result = _validate(_staged_app(tmp_path, project_root), project_root.parent, project_root)

    assert result.returncode == 2
    assert "ERROR SourceWorkspaceRoot changes the registered source-root mapping" in result.stdout
    assert "MISMATCH retained-history/source" in result.stdout
