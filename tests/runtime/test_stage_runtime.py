from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGE_SCRIPT = PROJECT_ROOT / "desktop" / "scripts" / "stage-runtime.ps1"
MAPPING_SCRIPT = PROJECT_ROOT / "desktop" / "scripts" / "validate-source-mapping.py"


def test_stage_runtime_builds_web_before_copying_dist() -> None:
    script = STAGE_SCRIPT.read_text(encoding="utf-8")
    build = "& pnpm --filter @research-asset-hub/web build"
    copy_dist = "Copy-Item -LiteralPath (Join-Path $projectRoot 'apps/web/dist')"

    assert build in script
    assert "if ($LASTEXITCODE -ne 0) { throw 'Web build failed' }" in script
    assert copy_dist in script
    assert script.index(build) < script.index(copy_dist)


def _write_sources(project_root: Path) -> None:
    (project_root / "config").mkdir(parents=True)
    (project_root / "config" / "sources.yaml").write_text(
        "workspace_root: ..\n"
        "sources:\n"
        "  - id: retained-history\n"
        "    root: 历史资料/只读归档\n"
        "    asset_roots:\n"
        "      pdf: 中文论文\n",
        encoding="utf-8",
    )


def _staged_app(tmp_path: Path, project_root: Path) -> Path:
    app_dir = tmp_path / "stage" / "app"
    shutil.copytree(project_root / "config", app_dir / "config")
    return app_dir


def _validate(
    app_dir: Path, workspace_root: Path, project_root: Path
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "HUB_HOST_WORKSPACE_ROOT": str(project_root.parent / "ignored"),
        "HUB_DATA_DIR": str(app_dir.parent / "fixture-managed-data"),
    }
    return subprocess.run(
        [
            sys.executable,
            str(MAPPING_SCRIPT),
            "--reference",
            str(project_root / "config" / "sources.yaml"),
            "--candidate",
            str(app_dir / "config" / "sources.yaml"),
            "--workspace-root",
            str(workspace_root),
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
    (project_root / "历史资料" / "只读归档").mkdir(parents=True)
    assert not (project_root / "历史资料" / "只读归档" / "中文论文").exists()
    result = _validate(_staged_app(tmp_path, project_root), project_root, project_root)

    assert result.returncode == 0, result.stderr
    assert "Immutable source root mappings verified" in result.stdout
    assert not (project_root / "历史资料" / "只读归档" / "中文论文").exists()


def test_root_validation_rejects_parent_directory_even_when_roots_are_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "核心领域" / "research-asset-hub"
    _write_sources(project_root)
    result = _validate(_staged_app(tmp_path, project_root), project_root.parent, project_root)

    assert result.returncode != 0
    assert "Copied sources configuration changes registered root mappings" in result.stderr
    assert "Immutable source root mappings verified" not in result.stdout
