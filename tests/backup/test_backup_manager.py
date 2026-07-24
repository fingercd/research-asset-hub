from __future__ import annotations

from pathlib import Path

import pytest

from packages.backup.manager import BackupManager
from packages.backup.workflow import wsl_path


class _Credentials:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self, account: str) -> str | None:
        return self.value

    def set(self, account: str, secret: str) -> None:
        self.value = secret


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
