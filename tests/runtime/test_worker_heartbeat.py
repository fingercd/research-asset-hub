from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from apps.worker import main as worker_main


def windows_error(code: int) -> OSError:
    error = PermissionError("heartbeat target is occupied")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32])
def test_heartbeat_retries_windows_sharing_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text('{"status":"old"}', encoding="utf-8")
    replace = Path.replace
    calls = 0
    sleep = Mock()

    def replace_after_reader_closes(source: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls <= 2:
            assert json.loads(path.read_text("utf-8")) == {"status": "old"}
            raise windows_error(code)
        return replace(source, target)

    monkeypatch.setattr(worker_main.sys, "platform", "win32")
    monkeypatch.setattr(worker_main.time, "sleep", sleep)
    monkeypatch.setattr(Path, "replace", replace_after_reader_closes)
    worker_main.write_heartbeat(path, status="idle", worker_id="worker-test")

    assert calls == 3
    assert [call.args for call in sleep.call_args_list] == [(0.05,), (0.05,)]
    heartbeat = json.loads(path.read_text("utf-8"))
    assert heartbeat["status"] == "idle"
    assert heartbeat["worker_id"] == "worker-test"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("code", [5, 32])
def test_heartbeat_exhausts_bounded_retry_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    path = tmp_path / "heartbeat.json"
    original = '{"status":"old"}'
    path.write_text(original, encoding="utf-8")
    error = windows_error(code)
    replace = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(worker_main.sys, "platform", "win32")
    monkeypatch.setattr(worker_main.time, "sleep", sleep)
    monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(PermissionError) as caught:
        worker_main.write_heartbeat(path, status="idle", worker_id="worker-test")

    assert caught.value is error
    assert replace.call_count == 10
    assert [call.args for call in sleep.call_args_list] == [(0.05,)] * 9
    assert sum(call.args[0] for call in sleep.call_args_list) < 1
    assert path.read_text("utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    ("platform", "error"),
    [
        ("linux", windows_error(5)),
        ("linux", windows_error(32)),
        ("win32", windows_error(87)),
        ("win32", PermissionError("permission denied without winerror")),
        ("win32", FileNotFoundError("target parent was removed")),
    ],
)
def test_heartbeat_does_not_retry_unrelated_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    error: OSError,
) -> None:
    path = tmp_path / "heartbeat.json"
    replace = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(worker_main.sys, "platform", platform)
    monkeypatch.setattr(worker_main.time, "sleep", sleep)
    monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(OSError) as caught:
        worker_main.write_heartbeat(path, status="idle", worker_id="worker-test")

    assert caught.value is error
    replace.assert_called_once_with(path)
    sleep.assert_not_called()
    assert not path.exists()
    assert list(tmp_path.glob(".*.tmp")) == []
