from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HUB_SCRIPT = PROJECT_ROOT / "scripts" / "hub.ps1"


def test_wsl_keepalive_uses_argument_safe_sleep_process() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "'sleep'," in script
    assert "'infinity'" in script
    assert "while true; do sleep 3600; done" not in script


def test_start_repairs_a_partial_recorded_process_set() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "$missingProcesses.Count -eq 0" in script
    assert "partial or stale process set; restarting it" in script
    assert "Stop-HubProcesses -Records $recordsToStop" in script
    assert "$script:wslKeepAliveRecord = $reusableKeepAlive[0]" in script


def test_local_runtime_uses_the_documented_three_port_contract() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "HUB_WEB_PORT' -Default 8765" in script
    assert "HUB_ASSET_GATEWAY_PORT' -Default 8766" in script
    assert "HUB_API_PORT' -Default 8767" in script
    assert "$env:HUB_WEB_ORIGIN = $webUrl" in script
    assert "$env:HUB_GATEWAY_ORIGIN = $assetGatewayUrl" in script


def test_start_waits_for_health_and_cleans_a_partial_start() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "Wait-HubReadiness -Records $started -Mode $Command" in script
    assert '"$resolvedApiUrl/api/health"' in script
    assert '"$resolvedGatewayUrl/health"' in script
    assert '"$resolvedWebUrl/health"' in script
    assert "startup failed and partial application services were cleaned up" in script
    assert "Stop-HubProcesses -Records $applicationRecords" in script


def test_stop_can_preserve_wsl_compose_infrastructure() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$KeepInfrastructure" in script
    assert "if ($KeepInfrastructure)" in script
    assert "Compose infrastructure was kept running." in script
    assert "Save-HubProcessState -Mode 'start' -Processes $keepAliveRecords" in script


def test_status_reports_runtime_urls_and_not_bootstrap_status() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "if ($Command -eq 'status')" in script
    assert "Write-HubRuntimeStatus -Runtime $runtime" in script
    assert "API docs:" in script
    assert "Asset health:" in script


def test_start_manages_gitnexus_service() -> None:
    script = HUB_SCRIPT.read_text(encoding="utf-8")

    assert "HUB_GITNEXUS_PORT' -Default 4747" in script
    assert "gitnexus-serve" in script
    assert "$env:HUB_GITNEXUS_URL = $gitnexusUrl" in script
    assert "Invoke-HubHealthProbe -Name 'gitnexus-serve'" in script
    assert "npm install -g gitnexus" in script
