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
    assert "Stop-HubProcessTree $record" in script
