"""Create a complete local platform backup staging set."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.domain.exporters.common import atomic_write_json

from .manager import BackupManager


def wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").casefold()
    suffix = resolved.as_posix().split(":", 1)[-1]
    return f"/mnt/{drive}{suffix}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory_paths(project_root: Path, paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """Return a deterministic inventory for files and directory trees.

    Restic already authenticates its packs, but this manifest gives the restore
    rehearsal a repository-independent way to prove that the open catalog and
    configuration tree survived intact.
    """

    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
    return {
        path.relative_to(project_root).as_posix(): {
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(files, key=lambda candidate: candidate.as_posix().casefold())
    }


def _tree_digest(files: dict[str, dict[str, Any]], prefix: str) -> str | None:
    selected = [
        (path, values)
        for path, values in files.items()
        if path == prefix or path.startswith(f"{prefix}/")
    ]
    if not selected:
        return None
    digest = hashlib.sha256()
    for path, values in selected:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(values["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(values["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _postgres_dump(project_root: Path, destination: Path) -> None:
    project_wsl = shlex.quote(wsl_path(project_root))
    command = (
        f"cd {project_wsl} && "
        "DOCKER_CONFIG=/tmp/rah-docker-config "
        "docker compose -f deploy/compose.yaml exec -T postgres "
        "pg_dump -U research_hub -d research_asset_hub -Fc"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        result = subprocess.run(
            ["wsl", "-d", "Ubuntu", "-u", "root", "--", "sh", "-lc", command],
            check=False,
            stdout=handle,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PostgreSQL custom dump failed: {message}")


def create_platform_backup(
    project_root: Path,
    manager: BackupManager | None = None,
) -> dict[str, Any]:
    """Dump PostgreSQL, hash the staging set and create a tagged Restic snapshot."""

    manager = manager or BackupManager()
    manager_status = manager.ensure_repository()
    timestamp = datetime.now(UTC)
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    staging = project_root / "work" / "backups" / stamp
    dump_path = staging / "postgres.dump"
    _postgres_dump(project_root, dump_path)

    include = [
        dump_path,
        project_root / "config",
        project_root / "outputs" / "open-catalog",
    ]
    existing = [path for path in include if path.exists()]
    files = _inventory_paths(project_root, existing)
    manifest = {
        "schema": "research-asset-hub.backup/v2",
        "created_at": timestamp.isoformat(),
        "backup_scope": [
            "postgres-custom-dump",
            "open-catalog",
            "configuration",
        ],
        "excluded_rebuildable": ["meilisearch-volume"],
        "risk": "local-copy-only",
        "files": files,
        "tree_digests": {
            "configuration": _tree_digest(files, "config"),
            "open_catalog": _tree_digest(files, "outputs/open-catalog"),
        },
    }
    manifest_path = atomic_write_json(staging / "manifest.json", manifest)
    snapshot = manager.backup(
        [*existing, manifest_path],
        tags=["research-asset-hub", "platform-v1"],
    )
    check = manager.check()
    manager.apply_retention()
    return {
        **manager_status,
        "status": "verified",
        "snapshot_id": snapshot.snapshot_id,
        "files_new": snapshot.files_new,
        "bytes_added": snapshot.bytes_added,
        "staging_manifest": str(manifest_path),
        "repository_check": check["status"],
        "risk": "local-copy-only",
    }
