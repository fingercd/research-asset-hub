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
        records = record_list(load_json(source.manifest))
        result["record_count"] = len(records)
        result["record_count_matches"] = (
            source.expected_records is None or len(records) == source.expected_records
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
    if not shutil.which("docker"):
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
            "docker": command_status("docker"),
        },
        "ports": {
            "web_8765_available": port_available("127.0.0.1", 8765),
            "asset_gateway_8766_available": port_available("127.0.0.1", 8766),
            "api_8767_available": port_available("127.0.0.1", 8767),
            "postgres_5432_available": port_available("127.0.0.1", 5432),
            "meilisearch_7700_available": port_available("127.0.0.1", 7700),
            "qdrant_6333_available": port_available("127.0.0.1", 6333),
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


def source_inventory(source: LoadedSource) -> dict[str, Any]:
    document = load_json(source.manifest)
    records = record_list(document)
    stat = source.manifest.stat()
    formal_patterns = source.raw.get("formal_html") or []
    asset_roots = source.raw.get("asset_roots") or {}
    resolved_asset_roots = {
        role: source.resolve_source_path(path)
        for role, path in asset_roots.items()
        if isinstance(path, str)
    }
    return {
        "id": source.source_id,
        "label": source.label,
        "domain": source.domain,
        "adapter": source.adapter,
        "root": str(source.root),
        "manifest": {
            "path": str(source.manifest),
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(source.manifest),
            "records": len(records),
        },
        "asset_roots": {
            role: {
                "path": str(path),
                "exists": path.exists(),
            }
            for role, path in resolved_asset_roots.items()
        },
        "formal_html": {
            "patterns": formal_patterns,
            "matched_files": sum(
                _safe_glob_count(source.root, pattern)
                for pattern in formal_patterns
                if isinstance(pattern, str)
            ),
        },
        "visible_assets": {
            "pdf_files": _extension_count(resolved_asset_roots.get("pdf", source.root), ".pdf"),
            "html_files_in_declared_html_root": _extension_count(
                resolved_asset_roots.get("html", source.root), ".html"
            ),
        },
        "read_only": source.read_only,
    }


def build_inventory_report(sources: list[LoadedSource]) -> dict[str, Any]:
    inventories: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source in sources:
        try:
            inventories.append(source_inventory(source))
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
