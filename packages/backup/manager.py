"""Restic wrapper that keeps its password in the operating-system keyring."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.integrations.credentials import CredentialStore

RESTIC_ACCOUNT = "restic-repository-password"


def default_backup_repository() -> Path:
    return Path.home() / "Documents" / "ResearchAssetHubBackups"


def _find_restic() -> Path:
    configured = os.environ.get("HUB_RESTIC_PATH", "").strip()
    app_root = Path(__file__).resolve().parents[2]
    for candidate in (
        Path(configured) if configured else None,
        app_root.parent / "runtime" / "restic.exe",
        app_root / "runtime" / "restic.exe",
    ):
        if candidate is not None and candidate.is_file():
            return candidate
    executable = shutil.which("restic")
    if executable:
        return Path(executable)
    package_root = (
        Path.home()
        / "AppData"
        / "Local"
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "restic.restic_Microsoft.Winget.Source_8wekyb3d8bbwe"
    )
    candidates = sorted(package_root.glob("restic*_windows_amd64.exe"))
    if candidates:
        return candidates[-1]
    raise FileNotFoundError("restic executable is not installed")


@dataclass(frozen=True, slots=True)
class BackupResult:
    snapshot_id: str | None
    repository: str
    files_new: int
    bytes_added: int
    local_copy_only: bool = True


class BackupManager:
    """Run Restic without placing a password in config files or command arguments."""

    def __init__(
        self,
        repository: Path | None = None,
        *,
        credential_store: CredentialStore | None = None,
        executable: Path | None = None,
    ) -> None:
        self.repository = (repository or default_backup_repository()).resolve()
        self.credential_store = credential_store or CredentialStore()
        self.executable = (executable or _find_restic()).resolve()

    def _password(self, *, create: bool) -> tuple[str, bool]:
        current = self.credential_store.get(RESTIC_ACCOUNT)
        if current:
            return current, False
        if not create:
            raise RuntimeError("Restic repository password is absent from credential manager")
        generated = secrets.token_urlsafe(48)
        self.credential_store.set(RESTIC_ACCOUNT, generated)
        return generated, True

    def _run(
        self,
        arguments: list[str],
        *,
        create_password: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        password, _ = self._password(create=create_password)
        environment = os.environ.copy()
        environment["RESTIC_REPOSITORY"] = str(self.repository)
        environment["RESTIC_PASSWORD"] = password
        result = subprocess.run(
            [str(self.executable), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"restic {' '.join(arguments[:2])} failed: {message}")
        return result

    def ensure_repository(self) -> dict[str, Any]:
        self.repository.mkdir(parents=True, exist_ok=True)
        _, password_created = self._password(create=True)
        probe = self._run(["snapshots", "--json"], check=False)
        initialized = probe.returncode == 0
        if not initialized:
            self._run(["init"])
        return {
            "repository": str(self.repository),
            "initialized": not initialized,
            "credential_created": password_created,
            "password_exposed": False,
            "risk": "local-copy-only",
        }

    def backup(self, paths: list[Path], *, tags: list[str] | None = None) -> BackupResult:
        if not paths:
            raise ValueError("at least one backup path is required")
        resolved = [path.resolve() for path in paths]
        for path in resolved:
            if not path.exists():
                raise FileNotFoundError(path)
            if path == self.repository or self.repository in path.parents:
                raise ValueError("the restic repository cannot back up itself")
        arguments = ["backup", "--json", "--tag", "local-copy-only"]
        for tag in tags or []:
            arguments.extend(["--tag", tag])
        arguments.extend(str(path) for path in resolved)
        result = self._run(arguments)
        summary: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("message_type") == "summary":
                summary = payload
        return BackupResult(
            snapshot_id=summary.get("snapshot_id"),
            repository=str(self.repository),
            files_new=int(summary.get("files_new") or 0),
            bytes_added=int(summary.get("data_added") or 0),
        )

    def check(self) -> dict[str, Any]:
        """Sample repository data; this does not verify an individual backup."""
        result = self._run(["check", "--read-data-subset=5%"])
        return {
            "status": "verified",
            "repository": str(self.repository),
            "output": result.stdout.strip(),
            "risk": "local-copy-only",
            "verification_scope": "repository-integrity",
            "data_read": "5%",
            "restore_rehearsal": "not_run",
        }

    def _resolve_snapshot(self, snapshot_id: str) -> str:
        result = self._run(["snapshots", "--json", snapshot_id])
        snapshots = json.loads(result.stdout)
        matches = [
            snapshot["id"]
            for snapshot in snapshots
            if snapshot["id"].casefold().startswith(snapshot_id.casefold())
        ]
        if len(matches) != 1:
            raise RuntimeError("the recorded Restic snapshot is missing or ambiguous")
        return str(matches[0])

    def verify_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Verify snapshot presence and all repository data, without restoring.

        Restic check validates the repository's snapshot trees and data packs.
        Reading all packs also covers this snapshot's data, but costs a full
        repository scan and does not prove that PostgreSQL can be restored.
        """
        if not re.fullmatch(r"[0-9a-fA-F]{8,64}", snapshot_id):
            raise ValueError("a recorded Restic snapshot ID is required")
        resolved_snapshot_id = self._resolve_snapshot(snapshot_id)
        result = self._run(["check", "--read-data"])
        # The snapshot may have been removed between the initial lookup and
        # check acquiring its repository lock. Observe its presence again when
        # verification finishes, using the full ID rather than a short prefix.
        self._resolve_snapshot(resolved_snapshot_id)
        return {
            "status": "verified",
            "snapshot_id": resolved_snapshot_id,
            "repository": str(self.repository),
            "repository_check": "verified",
            "verification_scope": "snapshot-storage-integrity",
            "data_read": "all-repository-data",
            "restore_rehearsal": "not_run",
            "output": result.stdout.strip(),
            "risk": "local-copy-only",
        }

    def apply_retention(self) -> None:
        self._run(
            [
                "forget",
                "--keep-daily",
                "7",
                "--keep-weekly",
                "4",
                "--keep-monthly",
                "6",
                "--keep-yearly",
                "6",
                "--prune",
            ]
        )

    def latest_snapshot(self) -> str:
        result = self._run(["snapshots", "--latest", "1", "--json"])
        values = json.loads(result.stdout)
        if not values:
            raise RuntimeError("no restic snapshots are available")
        return str(values[-1]["short_id"])

    def restore(self, snapshot_id: str, target: Path, *, allowed_root: Path) -> Path:
        target = target.resolve()
        allowed_root = allowed_root.resolve()
        try:
            target.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError("restore target must stay inside the declared restore root") from exc
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            raise ValueError("restore target must be empty")
        self._run(["restore", snapshot_id, "--target", str(target)])
        return target
