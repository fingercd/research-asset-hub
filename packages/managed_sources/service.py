"""Append-only source declarations in the user's data directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import yaml


def default_data_directory() -> Path:
    """Use the desktop's saved library choice for standalone API/CLI calls."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        preference = Path(local) / "ResearchAssetHub" / "data-directory.txt"
        if preference.is_file():
            selected = Path(preference.read_text(encoding="utf-8-sig").strip())
            if not selected.is_absolute():
                raise ValueError("保存的资料库目录必须是绝对路径")
            return selected
    return Path.home() / "Documents" / "ResearchAssetHub"


def source_workspace_root(config_path: Path, document: dict[str, Any]) -> Path:
    """Resolve the same registered workspace for desktop, API and CLI copies."""
    override = os.environ.get("HUB_HOST_WORKSPACE_ROOT", "").strip()
    hint = config_path.parent / "workspace-root.txt"
    if override:
        return Path(override).expanduser().resolve()
    if hint.is_file():
        path = Path(hint.read_text(encoding="utf-8-sig").strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("资料工作区提示必须是绝对路径")
        return path.resolve()
    return (config_path.parent / str(document.get("workspace_root", "."))).resolve()


def data_directory(settings: Any = None) -> Path:
    value = getattr(settings, "data_dir", None) if settings is not None else None
    value = value or os.environ.get("HUB_DATA_DIR")
    if not value:
        value = default_data_directory()
    return Path(value).expanduser().resolve()


def data_directory_fingerprint(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()


def managed_path(data_dir: Path, *parts: str) -> Path:
    """Reject redirects below a configured data root before writes or deletion."""
    base = Path(data_dir).expanduser().resolve()
    candidate = base.joinpath(*parts)
    if not candidate.resolve().is_relative_to(base):
        raise ValueError("托管数据路径越过资料目录边界")
    current = base
    for part in candidate.relative_to(base).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                raise ValueError("托管数据目录不能包含符号链接或目录重解析点")
    return candidate


@contextmanager
def file_lock(path: Path, *, timeout: float = 15) -> Iterator[None]:
    """OS locks release on process death; no stale lock-file recovery needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    lock_api = cast(Any, fcntl)
                    lock_api.flock(handle.fileno(), lock_api.LOCK_EX | lock_api.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("资料操作正由另一个进程完成，请稍后重试") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                lock_api = cast(Any, fcntl)
                lock_api.flock(handle.fileno(), lock_api.LOCK_UN)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def immutable_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="immutable-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("已登记资料内容不可覆盖，请创建新的来源") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_managed_sources(data_dir: Path) -> list[dict[str, Any]]:
    path = managed_path(data_dir, "config", "managed-sources.yaml")
    if not path.exists():
        return []
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise ValueError("托管来源注册表无效")
    if any(not isinstance(row, dict) for row in document["sources"]):
        raise ValueError("托管来源必须为对象")
    return cast(list[dict[str, Any]], document["sources"])


def managed_source_origin(source: dict[str, Any]) -> Path:
    """The immutable registration records its original data directory."""
    root = Path(source["root"])
    if root.parts[-3:] != ("config", "managed-sources", source["id"]):
        raise ValueError("托管来源清单目录与来源 ID 不一致")
    return root.parents[2]


def relocated_managed_path(value: str | Path, origin: Path, data_dir: Path) -> Path:
    """Relocate managed files when opening a restored library; keep external roots."""
    path = Path(value)
    for subtree in ("library", "config/managed-sources"):
        original = origin / subtree
        if path.is_relative_to(original):
            return data_dir / subtree / path.relative_to(original)
    return path


def register_managed_source(
    data_dir: Path, source: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Persist one immutable manifest and atomically append its root declaration.

    ``source`` supplies id/label/domain/adapter/asset_roots. The helper owns root,
    manifest and expected_records. Identical retries return the same declaration.
    """
    data_dir = Path(data_dir).expanduser().resolve()
    source_id = source["id"]
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", source_id):
        raise ValueError("来源 ID 无效")
    if source.get("read_only", True) is not True:
        raise ValueError("正式来源必须只读")
    roots = source.get("asset_roots", {})
    if not isinstance(roots, dict) or any(
        not Path(value).is_absolute() for value in roots.values()
    ):
        raise ValueError("托管来源 asset_roots 必须是受控绝对路径")
    root = managed_path(data_dir, "config", "managed-sources", source_id)
    declaration = {
        **source,
        "root": str(root),
        "manifest": "manifest.json",
        "read_only": True,
        "expected_records": len(manifest.get("items", [])),
    }
    with file_lock(managed_path(data_dir, "locks", "managed-sources.lock")):
        rows = load_managed_sources(data_dir)
        old = next((row for row in rows if row.get("id") == source_id), None)
        if old is not None and old != declaration:
            raise ValueError("已登记来源配置不可覆盖")
        immutable_write(
            managed_path(data_dir, "config", "managed-sources", source_id, "manifest.json"),
            canonical_bytes(manifest),
        )
        if old is None:
            rows.append(declaration)
            overlay = managed_path(data_dir, "config", "managed-sources.yaml")
            content = yaml.safe_dump(
                {"schema_version": 1, "sources": rows}, allow_unicode=True, sort_keys=False
            ).encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(
                prefix="managed-sources-", suffix=".tmp", dir=overlay.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, overlay)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return declaration


def frozen_source_config(source: dict[str, Any]) -> dict[str, Any]:
    from packages.domain.models import utc_now

    manifest = Path(source["root"]) / source["manifest"]
    return {
        **source,
        "_frozen_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "_frozen_source_config_sha256": hashlib.sha256(canonical_bytes(source)).hexdigest(),
        "_frozen_at": utc_now().isoformat(),
    }
