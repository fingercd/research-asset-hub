"""Create a complete local platform backup staging set."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
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


def _copy_backup_input(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return
    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in source.rglob("*"):
        if not item.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"backup input escapes managed root: {item}")
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


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
        if _native_pg_dump_path() is not None and os.environ.get("HUB_DATA_DIR"):
            result = subprocess.CompletedProcess([], 1, stderr=b"native desktop database selected")
        else:
            try:
                result = subprocess.run(
                    ["wsl", "-d", "Ubuntu", "-u", "root", "--", "sh", "-lc", command],
                    check=False,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                result = subprocess.CompletedProcess([], 1, stderr=b"WSL executable is absent")
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
            native_message = native_result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                "PostgreSQL custom dump failed through Compose and native pg_dump: "
                f"{compose_message}; {native_message}"
            )


def _native_pg_dump_path() -> Path | None:
    configured = os.environ.get("HUB_PG_DUMP_PATH", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[3] / "runtime" / "postgres" / "bin" / "pg_dump.exe",
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
    data_dir: Path | None = None,
    external_inputs: dict[str, Path] | None = None,
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
    data_root = (
        data_dir
        or Path(os.environ.get("HUB_DATA_DIR") or (Path.home() / "Documents" / "ResearchAssetHub"))
    ).resolve()
    staging_root = data_root / "backups" / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f"{stamp}-", dir=staging_root))
    dump_path = staging / "postgres.dump"
    _postgres_dump(project_root, dump_path)

    # Snapshot a self-contained tree: installation directories and registered
    # external source roots remain untouched. Never follow managed-library links
    # outside the data directory when selecting the backup's scope.
    configuration = data_root / "config"
    if not (configuration / "sources.yaml").is_file():
        configuration = project_root / "config"
    catalog = data_root / "outputs" / "open-catalog"
    if not catalog.is_dir() or not any(catalog.iterdir()):
        catalog = project_root / "outputs" / "open-catalog"
    inputs = {
        "config": configuration,
        "library": data_root / "library",
        "outputs/open-catalog": catalog,
    }
    selected_external: dict[str, str] = {}
    covered_external: list[Path] = []
    for key, original in sorted(
        (external_inputs or {}).items(), key=lambda item: len(item[1].parts)
    ):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
            raise ValueError("external backup keys must be safe registered identifiers")
        original = original.resolve()
        if not original.exists():
            raise FileNotFoundError(original)
        if any(
            original == covered or original.is_relative_to(covered) for covered in covered_external
        ):
            continue
        covered_external.append(original)
        relative = f"external/{key}"
        inputs[relative] = original
        selected_external[key] = relative
    for relative, source in inputs.items():
        _copy_backup_input(source, staging / relative)
    if configuration != data_root / "config":
        # Source development may store only managed-sources.yaml in data/config.
        # Include its additions alongside the immutable legacy source config.
        _copy_backup_input(data_root / "config", staging / "config")
    existing = [dump_path, *(staging / relative for relative in inputs)]
    files = _inventory_paths(staging, existing)
    manifest = {
        "schema": "research-asset-hub.backup/v3",
        "layout": "self-contained-managed-data",
        "created_at": timestamp.isoformat(),
        "backup_scope": [
            "postgres-custom-dump",
            "open-catalog",
            "configuration",
            "source-retirement-ledger",
            "managed-library",
            "annotations-and-user-state-in-postgres",
        ],
        "data_scope": scope,
        "retirement_history_included": True,
        "excluded_rebuildable": ["meilisearch-volume"],
        "excluded_external_originals": not bool(selected_external),
        "external_originals": selected_external,
        "external_originals_policy": (
            "Explicit registered external inputs included; other external originals excluded."
            if selected_external
            else "Registered external originals excluded; source roots remain read-only references."
        ),
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
        tags=["research-asset-hub", "platform-v3"],
    )
    if not snapshot.snapshot_id:
        raise RuntimeError("Restic completed without a snapshot ID")
    verification = manager.verify_snapshot(snapshot.snapshot_id)
    return {
        **manager_status,
        "status": "verified",
        "snapshot_id": verification["snapshot_id"],
        "files_new": snapshot.files_new,
        "bytes_added": snapshot.bytes_added,
        "staging_manifest": str(manifest_path),
        "repository_check": verification["repository_check"],
        "snapshot_verification": verification,
        "risk": "local-copy-only",
        "data_scope": scope,
        "retirement_history_included": True,
        "managed_library_included": True,
        "excluded_external_originals": not bool(selected_external),
        "external_originals": selected_external,
        "retention_applied": False,
    }
