"""Create a complete local platform backup staging set."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from packages.domain.exporters.common import atomic_write_json

from .manager import BackupManager

BackupScope = Literal["all"]


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
        compose_message = result.stderr.decode("utf-8", errors="replace").strip()
        native = _native_pg_dump_path()
        if native is None:
            raise RuntimeError(
                f"PostgreSQL custom dump failed: {compose_message}; "
                "no native pg_dump executable was found"
            )
        database_url = os.environ.get(
            "HUB_DATABASE_URL",
            "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub",
        )
        parsed = urlparse(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise RuntimeError("PostgreSQL custom dump failed: unsupported HUB_DATABASE_URL")
        database_name = parsed.path.lstrip("/")
        if not database_name:
            raise RuntimeError("PostgreSQL custom dump failed: database name is empty")
        environment = os.environ.copy()
        if parsed.password:
            environment["PGPASSWORD"] = unquote(parsed.password)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            native_result = subprocess.run(
                [
                    str(native),
                    "-h",
                    parsed.hostname,
                    "-p",
                    str(parsed.port or 5432),
                    "-U",
                    unquote(parsed.username or "research_hub"),
                    "-d",
                    database_name,
                    "-Fc",
                ],
                check=False,
                stdout=handle,
                stderr=subprocess.PIPE,
                env=environment,
            )
        if native_result.returncode != 0:
            destination.unlink(missing_ok=True)
            native_message = native_result.stderr.decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(
                "PostgreSQL custom dump failed through Compose and native pg_dump: "
                f"{compose_message}; {native_message}"
            )


def _native_pg_dump_path() -> Path | None:
    configured = os.environ.get("HUB_PG_DUMP_PATH", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(found) if (found := shutil.which("pg_dump")) else None,
        Path(r"C:\Program Files\ResearchAssetHub\runtime\postgres\bin\pg_dump.exe"),
    ]
    return next(
        (candidate.resolve() for candidate in candidates if candidate and candidate.is_file()),
        None,
    )


def create_platform_backup(
    project_root: Path,
    manager: BackupManager | None = None,
    *,
    scope: BackupScope = "all",
) -> dict[str, Any]:
    """Dump all PostgreSQL history and create a tagged Restic snapshot.

    Backups deliberately use ``scope=all``: excluding retired provenance would
    make a signed retirement impossible to audit or roll back after restore.
    """

    if scope != "all":
        raise ValueError("platform backups must use scope='all'")

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
            "source-retirement-ledger",
        ],
        "data_scope": scope,
        "retirement_history_included": True,
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
        "data_scope": scope,
        "retirement_history_included": True,
    }
