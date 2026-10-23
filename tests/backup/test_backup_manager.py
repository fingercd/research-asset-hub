from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from packages.backup.manager import BackupManager, default_backup_repository
from packages.backup.workflow import wsl_path


class _Credentials:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self, account: str) -> str | None:
        return self.value

    def set(self, account: str, secret: str) -> None:
        self.value = secret


def test_default_backup_repository_follows_selected_data_directory(tmp_path, monkeypatch):
    data = tmp_path / "workspace" / "ResearchAssetHubData"
    monkeypatch.setenv("HUB_DATA_DIR", str(data))
    assert default_backup_repository() == data.parent / "ResearchAssetHubBackups"


def test_restic_cache_follows_selected_data_directory(tmp_path, monkeypatch):
    data = tmp_path / "ResearchAssetHubData"
    monkeypatch.setenv("HUB_DATA_DIR", str(data))
    credentials = _Credentials()
    credentials.value = "test-only-password"
    manager = BackupManager(
        repository=tmp_path / "backups",
        credential_store=credentials,
        executable=tmp_path / "restic.exe",
    )
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    manager._run(["snapshots", "--json"])
    assert calls[0]["RESTIC_CACHE_DIR"] == str(data / "cache" / "restic")


def test_wsl_path_handles_windows_drive() -> None:
    value = wsl_path(Path("D:/Research Hub"))
    assert value.casefold().startswith("/mnt/d/")
    assert value.endswith("Research Hub")


def test_restore_rejects_target_outside_allowed_root(tmp_path: Path) -> None:
    executable = tmp_path / "restic.exe"
    executable.write_bytes(b"")
    manager = BackupManager(
        repository=tmp_path / "repository",
        credential_store=_Credentials(),  # type: ignore[arg-type]
        executable=executable,
    )
    with pytest.raises(ValueError, match="restore target"):
        manager.restore("latest", tmp_path.parent / "outside", allowed_root=tmp_path)


def snapshot_manager(tmp_path: Path) -> BackupManager:
    return BackupManager(
        repository=tmp_path / "selected-repository",
        credential_store=_Credentials(),  # type: ignore[arg-type]
        executable=tmp_path / "restic.exe",
    )


def test_verify_snapshot_checks_recorded_id_and_all_repository_data(tmp_path, monkeypatch):
    manager = snapshot_manager(tmp_path)
    full_id = "abcdef12" + "0" * 56
    calls = []

    def fake_run(arguments):
        calls.append(arguments)
        output = json.dumps([{"id": full_id}]) if arguments[0] == "snapshots" else "checked"
        return subprocess.CompletedProcess(arguments, 0, stdout=output)

    monkeypatch.setattr(manager, "_run", fake_run)
    result = manager.verify_snapshot("abcdef12")
    assert calls == [
        ["snapshots", "--json", "abcdef12"],
        ["check", "--read-data"],
        ["snapshots", "--json", full_id],
    ]
    assert result["snapshot_id"] == full_id
    assert result["repository"] == str(manager.repository)
    assert result["data_read"] == "all-repository-data"
    assert result["verification_scope"] == "snapshot-storage-integrity"
    assert result["restore_rehearsal"] == "not_run"


@pytest.mark.parametrize("snapshots", [[], [{"id": "12345678" + "0" * 56}]])
def test_verify_missing_snapshot_does_not_check_unrelated_repository_data(
    tmp_path, monkeypatch, snapshots
):
    manager = snapshot_manager(tmp_path)
    calls = []

    def fake_run(arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(snapshots))

    monkeypatch.setattr(manager, "_run", fake_run)
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        manager.verify_snapshot("abcdef12")
    assert calls == [["snapshots", "--json", "abcdef12"]]


@pytest.mark.parametrize("snapshot_id", ["", "latest", "--latest", "abc"])
def test_verify_rejects_snapshot_selectors(tmp_path, monkeypatch, snapshot_id):
    manager = snapshot_manager(tmp_path)
    monkeypatch.setattr(manager, "_run", lambda _: pytest.fail("invalid ID reached Restic"))
    with pytest.raises(ValueError, match="recorded Restic snapshot ID"):
        manager.verify_snapshot(snapshot_id)


def test_verify_snapshot_does_not_report_success_when_data_check_fails(tmp_path, monkeypatch):
    manager = snapshot_manager(tmp_path)

    def fake_run(arguments):
        if arguments[0] == "snapshots":
            return subprocess.CompletedProcess(
                arguments, 0, stdout=json.dumps([{"id": "abcdef12" + "0" * 56}])
            )
        raise RuntimeError("corrupt pack")

    monkeypatch.setattr(manager, "_run", fake_run)
    with pytest.raises(RuntimeError, match="corrupt pack"):
        manager.verify_snapshot("abcdef12")


def test_verify_snapshot_rejects_snapshot_removed_during_successful_data_check(
    tmp_path, monkeypatch
):
    manager = snapshot_manager(tmp_path)
    full_id = "abcdef12" + "0" * 56
    calls = []
    present = True

    def fake_run(arguments):
        nonlocal present
        calls.append(arguments)
        if arguments[0] == "check":
            present = False
            return subprocess.CompletedProcess(arguments, 0, stdout="repository is healthy")
        snapshots = [{"id": full_id}] if present else []
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(snapshots))

    monkeypatch.setattr(manager, "_run", fake_run)
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        manager.verify_snapshot("abcdef12")
    assert calls == [
        ["snapshots", "--json", "abcdef12"],
        ["check", "--read-data"],
        ["snapshots", "--json", full_id],
    ]
