from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HUB_SCRIPT = PROJECT_ROOT / "scripts" / "hub.ps1"
DESKTOP_SCRIPT = PROJECT_ROOT / "scripts" / "desktop.ps1"


def _powershell(code: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is required for executable command/AST checks")
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", code],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def _quoted(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/hub.ps1",
        "scripts/desktop.ps1",
        "scripts/bootstrap.ps1",
        "scripts/preflight.ps1",
        "desktop/scripts/stage-runtime.ps1",
    ],
)
def test_powershell_entrypoints_parse_without_errors(relative: str) -> None:
    result = _powershell(
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"{_quoted(PROJECT_ROOT / relative)}, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_hub_is_a_thin_sidecar_not_a_second_runtime_owner() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")
    assert "ValidateSet('preflight', 'inventory', 'ingest', 'validate', 'status')" in script
    assert "@('-m', 'packages.cli.main', $Command)" in script
    for flag in ("--all", "--dry-run", "--json", "--source"):
        assert flag in script
    for retired in (
        "Start-Process",
        "Stop-Process",
        "processes.json",
        "Invoke-WebRequest",
        "docker",
        "wsl",
    ):
        assert retired not in script
    assert "Get-Command python" not in script


@pytest.mark.parametrize("command", ["start", "dev", "stop"])
def test_retired_lifecycle_commands_fail_before_running_python(command: str) -> None:
    result = _powershell(f"& {_quoted(HUB_SCRIPT)} {command}")
    assert result.returncode != 0
    assert "ValidateSet" in result.stderr


def test_bootstrap_installs_dependencies_without_starting_infrastructure() -> None:
    script = (PROJECT_ROOT / "scripts/bootstrap.ps1").read_text(encoding="utf-8")
    assert "--frozen" in script
    assert "--frozen-lockfile" in script
    for retired in ("StartInfrastructure", "docker", "compose", "wsl", "hub.ps1 start"):
        assert retired not in script


def _development_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "源码 checkout"
    runtime = tmp_path / "portable runtime"
    data = tmp_path / "new isolated library"
    for base, relative in [
        (source, "desktop/src-tauri/Cargo.toml"),
        (source, ".venv/Scripts/python.exe"),
        *[
            (runtime, f"postgres/bin/{name}.exe")
            for name in ("initdb", "pg_ctl", "pg_isready", "psql")
        ],
        (runtime, "meilisearch.exe"),
    ]:
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return source, runtime, data


def test_native_plan_preserves_explicit_space_and_unicode_paths_without_starting(
    tmp_path: Path,
) -> None:
    source, runtime, data = _development_inputs(tmp_path)
    result = _powershell(
        f"& {_quoted(DESKTOP_SCRIPT)} plan -SourceRoot {_quoted(source)} "
        f"-RuntimeRoot {_quoted(runtime)} -DataDir {_quoted(data)}"
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["arguments"] == [
        "--dev-source-root",
        str(source.resolve()),
        "--dev-runtime-root",
        str(runtime.resolve()),
        "--data-dir",
        str(data.resolve()),
    ]
    assert plan["starts_services"] is False
    assert plan["native"][-2:] == ["--features", "custom-protocol"]
    assert plan["frontend"][-1] == "build"
    assert not data.exists()


def test_native_plan_rejects_implicit_data_directory(tmp_path: Path) -> None:
    source, runtime, _ = _development_inputs(tmp_path)
    result = _powershell(
        f"& {_quoted(DESKTOP_SCRIPT)} plan -SourceRoot {_quoted(source)} "
        f"-RuntimeRoot {_quoted(runtime)}"
    )
    assert result.returncode != 0
    assert "explicit RuntimeRoot and DataDir" in result.stderr


def test_native_build_runs_static_frontend_before_custom_protocol_cargo(tmp_path: Path) -> None:
    source, _, _ = _development_inputs(tmp_path)
    result = _powershell(
        "$global:calls = [Collections.Generic.List[object]]::new(); "
        "function global:pnpm { $global:calls.Add(@('pnpm') + $args); $global:LASTEXITCODE = 0 }; "
        "function global:cargo { $global:calls.Add(@('cargo') + $args); "
        "$global:LASTEXITCODE = 0 }; "
        f"& {_quoted(DESKTOP_SCRIPT)} build -SourceRoot {_quoted(source)}; "
        "$global:calls | ConvertTo-Json -Depth 4"
    )
    assert result.returncode == 0, result.stderr
    commands = json.loads(result.stdout)
    assert commands[0] == ["pnpm", "--filter", "@research-asset-hub/web", "build"]
    assert commands[1] == [
        "cargo",
        "build",
        "--manifest-path",
        str(source / "desktop/src-tauri/Cargo.toml"),
        "--features",
        "custom-protocol",
    ]


def test_native_entrypoint_never_guesses_installs_or_kills_existing_processes() -> None:
    script = DESKTOP_SCRIPT.read_text(encoding="utf-8")
    for retired in (
        "Start-Process",
        "Stop-Process",
        "taskkill",
        "Program Files",
        "vite preview",
        "tauri dev",
        "docker",
        "wsl",
    ):
        assert retired not in script
    assert "cargo metadata" in script
    assert "research-asset-hub-desktop.exe" in script


def test_development_build_restores_tauri_config_on_failure(tmp_path: Path) -> None:
    source, _, _ = _development_inputs(tmp_path)
    original = json.dumps({"bundle": {"active": False}, "app": {"withGlobalTauri": True}})
    result = _powershell(
        f"$env:TAURI_CONFIG = '{original}'; "
        "$global:original = $env:TAURI_CONFIG; "
        "function global:pnpm { $global:LASTEXITCODE = 0 }; "
        "function global:cargo { "
        "$config = ConvertFrom-Json -InputObject $env:TAURI_CONFIG; "
        "if ($null -ne $config.bundle.resources -or $config.bundle.active -ne $false "
        "-or $config.app.withGlobalTauri -ne $true) { throw 'bad development config' }; "
        "$global:LASTEXITCODE = 17 }; "
        f"try {{ & {_quoted(DESKTOP_SCRIPT)} build -SourceRoot {_quoted(source)} }} "
        "catch { if ($_.Exception.Message -ne 'Native debug build failed.') { throw } }; "
        "if ($env:TAURI_CONFIG -ne $global:original) { throw 'config was not restored' }; "
        "Write-Output 'restored'"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "restored"


def test_plan_lists_isolated_ports_and_cleared_production_settings(tmp_path: Path) -> None:
    source, runtime, data = _development_inputs(tmp_path)
    result = _powershell(
        f"& {_quoted(DESKTOP_SCRIPT)} plan -SourceRoot {_quoted(source)} "
        f"-RuntimeRoot {_quoted(runtime)} -DataDir {_quoted(data)}"
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["environment"] == {
        "HUB_API_PORT": "18767",
        "HUB_ASSET_GATEWAY_PORT": "18766",
        "HUB_POSTGRES_PORT": "15432",
        "HUB_MEILISEARCH_PORT": "17700",
    }
    assert {
        "HUB_DATABASE_URL",
        "DATABASE_URL",
        "HUB_SOURCES_CONFIG_PATH",
        "HUB_MEILISEARCH_API_KEY",
        "HUB_GATEWAY_SECRET",
    } <= set(plan["cleared_environment"])
    assert plan["tauri_config_override"] == {"bundle": {"resources": None}}


def test_plan_rejects_colliding_ports_without_starting(tmp_path: Path) -> None:
    source, runtime, data = _development_inputs(tmp_path)
    result = _powershell(
        f"& {_quoted(DESKTOP_SCRIPT)} plan -SourceRoot {_quoted(source)} "
        f"-RuntimeRoot {_quoted(runtime)} -DataDir {_quoted(data)} "
        "-ApiPort 15432 -PostgresPort 15432"
    )
    assert result.returncode != 0
    assert "must be distinct" in result.stderr
    assert not data.exists()
