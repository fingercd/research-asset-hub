from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NSIS = ROOT / "desktop/src-tauri/nsis"
HELPER = NSIS / "preinstall-upgrade.ps1"
VALIDATOR = NSIS / "preinstall-validate.py"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NSIS upgrade contract")


def _old_install(tmp_path: Path) -> tuple[Path, Path, Path]:
    install = tmp_path / "中文 空格安装"
    data = tmp_path / "用户数据"
    workspace = tmp_path / "原始来源工作区"
    originals = workspace / "外部论文"
    originals.mkdir(parents=True)
    (originals / "original.pdf").write_bytes(b"immutable-original")
    for source in (ROOT / "packages").rglob("*.py"):
        if "__pycache__" in source.parts:
            continue
        target = install / "app/packages" / source.relative_to(ROOT / "packages")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    registry = install / "app/apps/asset_gateway/registry.py"
    registry.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "apps/asset_gateway/registry.py", registry)
    (install / "app/apps/__init__.py").write_text("", encoding="utf-8")
    (registry.parent / "__init__.py").write_text("", encoding="utf-8")
    config = install / "app/config"
    config.mkdir()
    (config / "sources.yaml").write_text(
        "workspace_root: ..\nsources:\n  - id: frozen\n    root: 外部论文\n"
        "    read_only: true\n    asset_roots:\n      pdf: .\n",
        encoding="utf-8",
    )
    (config / "workspace-root.txt").write_text(str(workspace), encoding="utf-8-sig")
    (config / "python-executable.txt").write_text(sys.executable, encoding="utf-8")
    (install / "runtime/stale-pymupdf").mkdir(parents=True)
    (install / "runtime/stale-pymupdf/obsolete.pyd").write_bytes(b"old-library")
    (data / "postgres-data").mkdir(parents=True)
    (data / "postgres-data/do-not-touch").write_bytes(b"postgres-user-state")
    return install, data, workspace


def _run(install: Path, data: Path, *, script: Path = HELPER):
    environment = os.environ.copy()
    environment["HUB_DATA_DIR"] = str(data)
    environment.pop("HUB_HOST_WORKSPACE_ROOT", None)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallDir",
            str(install),
            "-MappingScript",
            str(VALIDATOR),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_upgrade_preserves_legacy_mapping_user_postgres_and_external_originals(tmp_path):
    install, data, workspace = _old_install(tmp_path)
    source_bytes = (install / "app/config/sources.yaml").read_bytes()
    result = _run(install, data)
    assert result.returncode == 0, result.stdout + result.stderr
    previous = list(install.glob(".previous-*"))
    assert len(previous) == 1
    assert not (install / "app").exists()
    assert not (install / "runtime").exists()
    assert (previous[0] / "runtime/stale-pymupdf/obsolete.pyd").read_bytes() == b"old-library"
    assert (data / "config/sources.yaml").read_bytes() == source_bytes
    assert (data / "config/workspace-root.txt").read_text(encoding="utf-8") == str(workspace)
    assert (data / "postgres-data/do-not-touch").read_bytes() == b"postgres-user-state"
    assert (workspace / "外部论文/original.pdf").read_bytes() == b"immutable-original"


def test_existing_user_config_is_reused_without_overwriting(tmp_path):
    install, data, _ = _old_install(tmp_path)
    config = data / "config"
    config.mkdir()
    source_bytes = b"workspace_root: ..\nsources: []\n# user-owned\n"
    (config / "sources.yaml").write_bytes(source_bytes)
    (config / "managed-sources.yaml").write_bytes(b"sources: []\n# user-owned-managed-settings\n")
    result = _run(install, data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (config / "sources.yaml").read_bytes() == source_bytes
    assert (
        config / "managed-sources.yaml"
    ).read_bytes() == b"sources: []\n# user-owned-managed-settings\n"


def test_managed_only_configuration_aborts_without_moving_resources(tmp_path):
    install, data, _ = _old_install(tmp_path)
    config = data / "config"
    config.mkdir()
    (config / "managed-sources.yaml").write_bytes(b"user-owned-managed-settings")
    result = _run(install, data)
    assert result.returncode != 0
    assert "managed-only" in result.stdout
    assert (install / "app").exists() and (install / "runtime").exists()
    assert not list(install.glob(".previous-*"))
    assert not (config / "sources.yaml").exists()


def test_data_directory_inside_resources_is_rejected_case_insensitively(tmp_path):
    install, _, _ = _old_install(tmp_path)
    data = Path(str(install / "APP/用户数据").swapcase())
    result = _run(install, data)
    assert result.returncode != 0
    assert "inside installation resources" in result.stdout
    assert (install / "app").exists() and (install / "runtime").exists()


def test_registered_originals_inside_resources_abort_even_when_root_is_missing(tmp_path):
    install, data, _ = _old_install(tmp_path)
    config = install / "app/config"
    (config / "workspace-root.txt").write_text(str(install), encoding="utf-8")
    (config / "sources.yaml").write_text(
        "workspace_root: ..\nsources:\n  - id: internal\n    root: runtime/originals\n"
        "    read_only: true\n",
        encoding="utf-8-sig",
    )
    result = _run(install, data)
    assert result.returncode != 0
    assert (install / "app").exists() and (install / "runtime").exists()
    assert not list(install.glob(".previous-*"))


def test_failed_second_resource_rename_rolls_back_first_and_keeps_user_data(tmp_path):
    install, data, workspace = _old_install(tmp_path)
    launcher = tmp_path / "rollback-fixture.ps1"
    escaped = str(HELPER).replace("'", "''")
    launcher.write_text(
        "param([string]$InstallDir, [string]$MappingScript)\n"
        f". '{escaped}' -InstallDir $InstallDir -MappingScript $MappingScript\n"
        "function Move-RahResource([string]$Source,[string]$Destination) {\n"
        "  if ((Split-Path -Leaf $Source) -eq 'runtime') { throw 'fixture locked runtime' }\n"
        "  Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop\n}\n"
        "try { Invoke-RahPreinstall $InstallDir $MappingScript; exit 0 }\n"
        "catch { Write-Output $_.Exception.Message; exit 1 }\n",
        encoding="utf-8-sig",
    )
    result = _run(install, data, script=launcher)
    assert result.returncode != 0
    assert "fixture locked runtime" in result.stdout
    assert (install / "app/config/sources.yaml").exists()
    assert (install / "runtime/stale-pymupdf/obsolete.pyd").exists()
    assert not list(install.glob(".previous-*/app"))
    assert (data / "config/sources.yaml").exists()
    assert (data / "postgres-data/do-not-touch").read_bytes() == b"postgres-user-state"
    assert (workspace / "外部论文/original.pdf").read_bytes() == b"immutable-original"


def test_fresh_install_does_not_require_python_or_create_user_configuration(tmp_path):
    install = tmp_path / "fresh-install"
    data = tmp_path / "user-data"
    result = _run(install, data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not install.exists() and not data.exists()


def test_upgrade_does_not_require_original_source_workspace_to_exist(tmp_path):
    install, data, workspace = _old_install(tmp_path)
    (workspace / "外部论文/original.pdf").unlink()
    (workspace / "外部论文").rmdir()
    workspace.rmdir()
    result = _run(install, data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / "config/workspace-root.txt").read_text(encoding="utf-8") == str(workspace)
    assert not workspace.exists()


def test_legacy_python_prefers_bundle_and_needs_no_executable_hint(tmp_path):
    install = tmp_path / "legacy-install"
    bundled = install / "runtime/python/python.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"fixture-not-executed")
    launcher = tmp_path / "bundle-python-fixture.ps1"
    escaped = str(HELPER).replace("'", "''")
    launcher.write_text(
        "param([string]$InstallDir,[string]$MappingScript)\n"
        f". '{escaped}' -InstallDir $InstallDir -MappingScript $MappingScript\n"
        "$selected = Get-RahLegacyPython $InstallDir\n"
        "if ($selected -ne (Join-Path $InstallDir 'runtime/python/python.exe')) { exit 1 }\n"
        "Write-Output 'Bundled interpreter selected without a hint'\n",
        encoding="utf-8-sig",
    )
    result = _run(install, tmp_path / "data", script=launcher)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bundled interpreter selected without a hint" in result.stdout


def test_data_directory_through_junction_aborts_without_mutating_originals(tmp_path):
    install, data, workspace = _old_install(tmp_path)
    junction = tmp_path / "data-alias"
    create_junction = tmp_path / "create-junction.ps1"
    create_junction.write_text(
        "param([string]$Alias,[string]$Target)\n"
        "$ErrorActionPreference='Stop'\n"
        "New-Item -ItemType Junction -Path $Alias -Target $Target | Out-Null\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(create_junction),
            str(junction),
            str(data),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Fixture junction creation is unavailable")
    result = _run(install, junction / "nested-data")
    assert result.returncode != 0
    assert "junctions or symbolic links" in result.stdout
    assert (install / "app").exists() and (install / "runtime").exists()
    assert (workspace / "外部论文/original.pdf").read_bytes() == b"immutable-original"


def test_nsis_preinstall_hook_compiles_with_bundled_helpers(tmp_path):
    compiler = Path(os.environ["LOCALAPPDATA"]) / "tauri/NSIS/makensis.exe"
    if not compiler.exists():
        pytest.skip("Local NSIS compiler not installed")
    installer = tmp_path / "upgrade-hook-fixture.nsi"
    output = tmp_path / "upgrade-hook-fixture.exe"
    installer.write_text(
        '!include "LogicLib.nsh"\n'
        f'!include "{NSIS / "installer-hooks.nsh"}"\n'
        f'OutFile "{output}"\nRequestExecutionLevel user\n'
        'Section "fixture"\n!insertmacro NSIS_HOOK_PREINSTALL\nSectionEnd\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(compiler), "/INPUTCHARSET", "UTF8", str(installer)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.is_file()
    assert "/REBOOTOK" not in (NSIS / "installer-hooks.nsh").read_text(encoding="utf-8")


def test_custom_template_is_exactly_the_pinned_upstream_plus_recorded_patches():
    metadata = json.loads((NSIS / "installer-template-source.json").read_text(encoding="utf-8"))
    upstream = (NSIS / "installer.upstream.nsi").read_bytes()
    assert hashlib.sha256(upstream).hexdigest() == metadata["sha256"]
    expected = upstream.decode("utf-8")
    for patch in metadata["patches"]:
        assert expected.count(patch["anchor"]) == 1
        expected = expected.replace(patch["anchor"], patch["replacement"])
    assert (NSIS / "installer.nsi").read_text(encoding="utf-8") == expected
    config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["windows"]["nsis"]["template"] == "nsis/installer.nsi"
    entry = expected.index("!insertmacro RAH_HOOK_REINSTALL_PAGE")
    assert expected.index('ReadRegStr $R1 SHCTX "${UNINSTKEY}" "UninstallString"') < entry
    assert expected.index('nsis_tauri_utils::SemverCompare "${VERSION}" $R0') < entry
    assert entry < expected.index('StrCpy $R2 "$(uninstallBeforeInstalling)"')
    install_section = expected[expected.index("Section Install\n") :]
    assert install_section.index("!insertmacro CheckIfAppIsRunning") < install_section.index(
        "!insertmacro NSIS_HOOK_PREINSTALL"
    )


def test_plain_upgrade_entry_sets_update_mode_and_aborts_before_uninstall_side_effects(tmp_path):
    compiler = Path(os.environ["LOCALAPPDATA"]) / "tauri/NSIS/makensis.exe"
    if not compiler.exists():
        pytest.skip("Local NSIS compiler not installed")
    script = tmp_path / "plain-entry-fixture.nsi"
    output = tmp_path / "plain-entry-fixture.exe"
    marker = tmp_path / "entry-mode.txt"
    forbidden = tmp_path / "old-uninstaller-called.txt"
    script.write_text(
        '!include "LogicLib.nsh"\n'
        f'!include "{NSIS / "installer-hooks.nsh"}"\n'
        '!define ALLOWDOWNGRADES "true"\n'
        f'OutFile "{output}"\nRequestExecutionLevel user\n'
        "Var UpdateMode\nVar WixMode\n"
        "Function .onInstFailed\n"
        f'FileOpen $0 "{marker}" w\n'
        'FileWrite $0 "$UpdateMode"\nFileClose $0\nFunctionEnd\n'
        'Section "fixture"\nStrCpy $WixMode 0\nStrCpy $R0 1\n'
        "!insertmacro RAH_HOOK_REINSTALL_PAGE\n"
        f'FileOpen $0 "{forbidden}" w\nFileClose $0\nSectionEnd\n',
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [str(compiler), "/INPUTCHARSET", "UTF8", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    subprocess.run([str(output), "/S"], check=False, timeout=30)
    assert marker.read_text(encoding="utf-8") == "1"
    assert not forbidden.exists()
    hook = (NSIS / "installer-hooks.nsh").read_text(encoding="utf-8")
    entry_macro = hook.split("!macro RAH_HOOK_REINSTALL_PAGE", 1)[1].split("!macroend", 1)[0]
    assert "Exec" not in entry_macro and "Rename" not in entry_macro and "File" not in entry_macro


def test_patched_full_tauri_template_compiles_without_packaging_real_resources(tmp_path):
    compiler = Path(os.environ["LOCALAPPDATA"]) / "tauri/NSIS/makensis.exe"
    generated = ROOT / "desktop/src-tauri/target/release/nsis/x64/installer.nsi"
    if not compiler.exists() or not generated.exists():
        pytest.skip("Full local Tauri rendering/NSIS compiler unavailable")
    metadata = json.loads((NSIS / "installer-template-source.json").read_text(encoding="utf-8"))
    rendered = generated.read_text(encoding="utf-8")
    for patch in metadata["patches"]:
        if patch["replacement"] in rendered:
            continue
        assert rendered.count(patch["anchor"]) == 1
        rendered = rendered.replace(patch["anchor"], patch["replacement"])
    # Parse/compile the complete wizard, callbacks, gates and uninstaller. Avoid
    # copying hundreds of MB of live build resources into the fixture executable.
    rendered = re.sub(
        r"^\s*File\s+.*$", "; fixture: resource payload omitted", rendered, flags=re.M
    )
    output = tmp_path / "full-template-fixture.exe"
    rendered = rendered.replace('!define OUTFILE "nsis-output.exe"', f'!define OUTFILE "{output}"')
    rendered = rendered.replace(
        '!define PRODUCTNAME "ResearchAssetHub"',
        '!define PRODUCTNAME "ResearchAssetHubTemplateFixture"',
    )
    fixture = tmp_path / "full-template-fixture.nsi"
    fixture.write_text(rendered, encoding="utf-8")
    result = subprocess.run(
        [
            str(compiler),
            "/INPUTCHARSET",
            "UTF8",
            f'/X!addincludedir "{generated.parent}"',
            str(fixture),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.is_file()
