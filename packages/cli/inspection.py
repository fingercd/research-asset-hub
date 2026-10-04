from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.path_exclusions import OperationalPathExclusions
from packages.managed_sources.service import canonical_bytes

from .config import PROJECT_ROOT, LoadedSource


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def record_list(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("papers", "items", "records", "assets"):
            value = document.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("Manifest root must be an array or contain papers/items/records/assets")


def _manifest_record_count(source: LoadedSource, document: Any) -> int:
    if source.adapter != "managed_repository":
        return len(record_list(document))
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("source_kind") not in {"user_selected_directory", "public_commit_archive"}
        or not isinstance(document.get("source_record_id"), str)
        or not document["source_record_id"]
    ):
        raise ValueError("Managed repository manifest requires its native source identity")
    # This provenance manifest describes a registered repository representation;
    # it is not an adapter record batch and creates no SourceRecord rows.
    return 0


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def command_status(name: str, version_args: list[str] | None = None) -> dict[str, Any]:
    executable = shutil.which(name)
    if executable is None:
        return {"available": False, "path": None, "version": None}
    args = [executable, *(version_args or ["--version"])]
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        version = (result.stdout or result.stderr).strip().splitlines()
        return {
            "available": True,
            "path": executable,
            "version": version[0] if version else None,
            "exit_code": result.returncode,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "available": True,
            "path": executable,
            "version": None,
            "error": str(exc),
        }


def docker_status() -> dict[str, Any]:
    """Find either a native Docker CLI or the project's supported WSL backend."""

    native = command_status("docker")
    if native["available"]:
        native["backend"] = "native"
        return native
    if os.name != "nt":
        return native
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if wsl is None:
        return native
    try:
        result = subprocess.run(
            [
                wsl,
                "-d",
                "Ubuntu",
                "-u",
                "root",
                "--",
                "sh",
                "-lc",
                "docker version --format '{{.Server.Version}}' "
                "&& docker compose version --short",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            **native,
            "backend": "unavailable",
            "wsl_error": str(exc),
        }
    versions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(versions) < 2:
        return {
            **native,
            "backend": "unavailable",
            "wsl_error": (result.stderr or result.stdout).strip() or None,
        }
    return {
        "available": True,
        "path": "wsl://Ubuntu/usr/bin/docker",
        "version": versions[0],
        "compose_version": versions[1],
        "exit_code": result.returncode,
        "backend": "wsl",
    }


def port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def windows_long_paths_enabled() -> bool | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        return bool(value)
    except OSError:
        return False


def source_preflight(source: LoadedSource) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": source.source_id,
        "label": source.label,
        "domain": source.domain,
        "adapter": source.adapter,
        "root": str(source.root),
        "manifest": str(source.manifest),
        "read_only": source.read_only,
        "root_exists": source.root.is_dir(),
        "manifest_exists": source.manifest.is_file(),
        "expected_records": source.expected_records,
        "record_count": None,
        "record_count_matches": None,
        "errors": [],
        "warnings": [],
    }
    if not source.read_only:
        result["errors"].append("source_must_be_read_only")
    if not source.root.is_dir():
        result["errors"].append("source_root_missing")
        return result
    if not source.manifest.is_file():
        result["errors"].append("manifest_missing")
        return result

    try:
        record_count = _manifest_record_count(source, load_json(source.manifest))
        result["record_count"] = record_count
        result["record_count_matches"] = (
            source.expected_records is None or record_count == source.expected_records
        )
        if not result["record_count_matches"]:
            result["warnings"].append("record_count_changed")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["errors"].append(f"manifest_invalid:{exc}")
    return result


def build_preflight_report(sources: list[LoadedSource]) -> dict[str, Any]:
    usage = shutil.disk_usage(PROJECT_ROOT)
    source_reports = [source_preflight(source) for source in sources]
    long_paths_enabled = windows_long_paths_enabled()
    blocking_errors = [
        {"source": report["id"], "error": error}
        for report in source_reports
        for error in report["errors"]
    ]
    warnings = ["external_backup_not_configured"]
    docker = docker_status()
    if not docker["available"]:
        warnings.append("docker_missing_infrastructure_services_unavailable")
    if long_paths_enabled is False:
        warnings.append("windows_long_paths_not_enabled")
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "project_root": str(PROJECT_ROOT),
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "supported": sys.version_info >= (3, 11),
        },
        "commands": {
            "git": command_status("git"),
            "rg": command_status("rg"),
            "node": command_status("node"),
            "pnpm": command_status("pnpm"),
            "docker": docker,
        },
        "ports": {
            "web_8765_available": port_available("127.0.0.1", 8765),
            "asset_gateway_8766_available": port_available("127.0.0.1", 8766),
            "api_8767_available": port_available("127.0.0.1", 8767),
            "postgres_5432_available": port_available("127.0.0.1", 5432),
            "meilisearch_7700_available": port_available("127.0.0.1", 7700),
        },
        "disk": {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "free_gib": round(usage.free / (1024**3), 2),
        },
        "filesystem": {
            "unicode_source_paths_readable": all(
                source.root.is_dir() and source.manifest.is_file() for source in sources
            ),
            "windows_long_paths_enabled": long_paths_enabled,
        },
        "sources": source_reports,
        "blocking_errors": blocking_errors,
        "warnings": warnings,
        "status": "failed" if blocking_errors else "ready_with_warnings",
    }


def _safe_glob_count(root: Path, pattern: str) -> int:
    try:
        return sum(1 for candidate in root.glob(pattern) if candidate.is_file())
    except (OSError, ValueError):
        return 0


def _extension_count(root: Path, extension: str) -> int:
    if not root.exists():
        return 0
    return sum(
        1 for path in root.rglob(f"*{extension}") if path.is_file() and ".git" not in path.parts
    )


def _declared_extension_count(
    *,
    source_root: Path,
    asset_roots: dict[str, Path],
    role: str,
    extension: str,
    ignored_root_keys: frozenset[str] = frozenset(),
) -> int:
    if role in ignored_root_keys:
        return 0
    declared_root = asset_roots.get(role)
    if declared_root is not None:
        return _extension_count(declared_root, extension)
    if asset_roots:
        return 0
    return _extension_count(source_root, extension)


def _glob_is_within_root(source: LoadedSource, pattern: str, root: Path) -> bool:
    """Identify an excluded declared root before ``Path.glob`` can traverse it."""

    prefix = pattern.replace("\\", "/").split("*")[0].rstrip("/")
    candidate = source.resolve_source_path(prefix or ".")
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def source_inventory(
    source: LoadedSource,
    *,
    operational_registry: SourceRegistry | None = None,
    applied_rules: set[str] | None = None,
) -> dict[str, Any]:
    document = load_json(source.manifest)
    record_count = _manifest_record_count(source, document)
    stat = source.manifest.stat()
    formal_patterns = source.raw.get("formal_html") or []
    asset_roots = source.raw.get("asset_roots") or {}
    resolved_asset_roots = {
        role: source.resolve_source_path(path)
        for role, path in asset_roots.items()
        if isinstance(path, str)
    }
    policy: OperationalPathExclusions | None = (
        getattr(operational_registry, "operational_exclusions", None)
        if operational_registry is not None
        else None
    )
    registry_roots = (
        {
            (item.source_id, item.root_key): item.path
            for item in operational_registry.describe()
        }
        if operational_registry is not None
        else {}
    )
    active_root_rules: dict[str, Any] = {}
    if policy is not None:
        for root_key in resolved_asset_roots:
            if (source.source_id, root_key) not in registry_roots:
                continue
            rule = policy.active_root(source.source_id, root_key, registry_roots)
            if rule is not None:
                active_root_rules[root_key] = rule
                if applied_rules is not None:
                    applied_rules.add(rule.id)
    ignored_root_keys = frozenset(active_root_rules)
    skipped_formal_patterns = [
        pattern
        for pattern in formal_patterns
        if isinstance(pattern, str)
        and any(
            _glob_is_within_root(source, pattern, resolved_asset_roots[root_key])
            for root_key in ignored_root_keys
        )
    ]
    return {
        "id": source.source_id,
        "label": source.label,
        "domain": source.domain,
        "adapter": source.adapter,
        "source_config_sha256": hashlib.sha256(canonical_bytes(source.raw)).hexdigest(),
        "root": str(source.root),
        "manifest": {
            "path": str(source.manifest),
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(source.manifest),
            "records": record_count,
        },
        "asset_roots": {
            role: {
                "path": str(path),
                "exists": path.exists(),
                "operational_exclusion": (
                    {
                        "rule_id": active_root_rules[role].id,
                        "reason": active_root_rules[role].reason,
                    }
                    if role in active_root_rules
                    else None
                ),
            }
            for role, path in resolved_asset_roots.items()
        },
        "formal_html": {
            "patterns": formal_patterns,
            "matched_files": sum(
                _safe_glob_count(source.root, pattern)
                for pattern in formal_patterns
                if isinstance(pattern, str) and pattern not in skipped_formal_patterns
            ),
            "skipped_operational_patterns": skipped_formal_patterns,
        },
        "visible_assets": {
            "pdf_files": _declared_extension_count(
                source_root=source.root,
                asset_roots=resolved_asset_roots,
                role="pdf",
                extension=".pdf",
                ignored_root_keys=ignored_root_keys,
            ),
            "html_files_in_declared_html_root": _declared_extension_count(
                source_root=source.root,
                asset_roots=resolved_asset_roots,
                role="html",
                extension=".html",
                ignored_root_keys=ignored_root_keys,
            ),
        },
        "read_only": source.read_only,
    }


def build_inventory_report(
    sources: list[LoadedSource],
    *,
    operational_registry: SourceRegistry | None = None,
) -> dict[str, Any]:
    inventories: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    applied_rules: set[str] = set()
    policy: OperationalPathExclusions | None = (
        getattr(operational_registry, "operational_exclusions", None)
        if operational_registry is not None
        else None
    )
    for source in sources:
        try:
            inventories.append(
                source_inventory(
                    source,
                    operational_registry=operational_registry,
                    applied_rules=applied_rules,
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"source": source.source_id, "error": str(exc)})
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "project_root": str(PROJECT_ROOT),
        "sources": inventories,
        "totals": {
            "source_count": len(inventories),
            "source_record_count": sum(item["manifest"]["records"] for item in inventories),
            "declared_pdf_files": sum(item["visible_assets"]["pdf_files"] for item in inventories),
            "declared_html_files": sum(
                item["formal_html"]["matched_files"] for item in inventories
            ),
        },
        "errors": errors,
        "operational_path_exclusions": (
            policy.summary(applied_rules) if policy is not None else None
        ),
        "status": "failed" if errors else "passed",
    }


def write_report(report: dict[str, Any], filename: str) -> Path:
    output_dir = PROJECT_ROOT / "work" / "validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, output_path)
    return output_path
