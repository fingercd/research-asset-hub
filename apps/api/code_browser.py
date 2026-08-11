"""Safe, read-only access to locally materialized code repositories."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from apps.api.editor import resolve_editor_path
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.models import Representation

MAX_PREVIEW_BYTES = 512 * 1024
MAX_DIRECTORY_ENTRIES = 400

_SENSITIVE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".sandbox-secrets",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "token.json",
    }
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_LANGUAGES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".md": "markdown",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".svg": "xml",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".vue": "vue",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_SPECIAL_LANGUAGES = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "requirements.txt": "text",
}


class CodeBrowserError(ValueError):
    """Base error raised for a code browser request."""


class CodeBrowserUnavailable(CodeBrowserError):
    """The representation is not backed by a safe local project directory."""


class CodeBrowserPathError(CodeBrowserError):
    """The requested path is invalid, hidden, or not a readable file/directory."""


def _relative_path(value: str | None) -> str:
    normalized = (value or "").replace("\\", "/").strip("/")
    if not normalized:
        return ""
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CodeBrowserPathError("代码路径无效。")
    return candidate.as_posix()


def _is_hidden_or_sensitive(path: Path) -> bool:
    name = path.name.casefold()
    return path.name.startswith(".") or name in _SENSITIVE_NAMES


def _is_ignored_directory(path: Path) -> bool:
    return path.name.casefold() in _IGNORED_DIRECTORIES


def language_for_path(path: str | Path) -> str:
    name = Path(path).name.casefold()
    if name in _SPECIAL_LANGUAGES:
        return _SPECIAL_LANGUAGES[name]
    return _LANGUAGES.get(Path(path).suffix.casefold(), "text")


def local_code_repository(
    registry: SourceRegistry,
    representation: Representation,
    *,
    source_alias: str | None,
) -> Path:
    """Resolve a code representation to a directory within a registered root."""

    resolution = resolve_editor_path(
        registry,
        representation,
        source_alias=source_alias,
    )
    if not resolution.available or not resolution.path:
        raise CodeBrowserUnavailable(resolution.reason or "源码未下载/未开源")
    try:
        root = registry.root_for(source_alias or "", representation.root_key or "repository")
    except UnsafeAssetPath as error:
        raise CodeBrowserUnavailable("源码路径未登记。") from error
    try:
        root = root.resolve(strict=True)
        candidate = Path(resolution.path).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as error:
        raise CodeBrowserUnavailable("源码路径未登记或已失效。") from error
    if not candidate.is_dir():
        raise CodeBrowserUnavailable("代码项目目录不可用。")
    return candidate


def _safe_child(root: Path, relative_path: str) -> Path:
    relative = _relative_path(relative_path)
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise CodeBrowserPathError("代码路径越过项目根目录。") from error
    if _is_hidden_or_sensitive(candidate):
        raise CodeBrowserPathError("隐藏或敏感文件不可预览。")
    return candidate


def list_code_directory(
    root: Path,
    *,
    representation_id: str,
    relative_path: str | None = None,
) -> dict[str, Any]:
    directory = _safe_child(root, relative_path or "")
    if not directory.is_dir():
        raise CodeBrowserPathError("代码目录不存在。")
    try:
        children = [
            child
            for child in directory.iterdir()
            if not _is_hidden_or_sensitive(child)
            and not (child.is_dir() and _is_ignored_directory(child))
        ]
    except OSError as error:
        raise CodeBrowserPathError("代码目录无法读取。") from error
    children.sort(key=lambda child: (not child.is_dir(), child.name.casefold()))
    entries: list[dict[str, Any]] = []
    current = _relative_path(relative_path)
    for child in children[:MAX_DIRECTORY_ENTRIES]:
        child_relative = PurePosixPath(current).joinpath(child.name).as_posix()
        if current == "":
            child_relative = child.name
        try:
            size_bytes = child.stat().st_size if child.is_file() else None
        except OSError:
            size_bytes = None
        entries.append(
            {
                "name": child.name,
                "path": child_relative,
                "kind": "directory" if child.is_dir() else "file",
                "size_bytes": size_bytes,
                "language": language_for_path(child) if child.is_file() else None,
            }
        )
    return {
        "representation_id": representation_id,
        "repository_name": root.name,
        "path": current,
        "entries": entries,
        "truncated": len(children) > MAX_DIRECTORY_ENTRIES,
    }


def read_code_file(
    root: Path,
    *,
    representation_id: str,
    relative_path: str,
) -> dict[str, Any]:
    file_path = _safe_child(root, relative_path)
    if not file_path.is_file():
        raise CodeBrowserPathError("代码文件不存在。")
    try:
        size_bytes = file_path.stat().st_size
        payload = file_path.read_bytes()[: MAX_PREVIEW_BYTES + 1]
    except OSError as error:
        raise CodeBrowserPathError("代码文件无法读取。") from error
    if b"\x00" in payload:
        return {
            "representation_id": representation_id,
            "path": _relative_path(relative_path),
            "language": "binary",
            "size_bytes": size_bytes,
            "line_count": 0,
            "content": None,
            "previewable": False,
            "truncated": False,
            "reason": "二进制文件不提供站内文本预览。",
        }
    truncated = len(payload) > MAX_PREVIEW_BYTES
    try:
        content = payload[:MAX_PREVIEW_BYTES].decode("utf-8")
        content = content.replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return {
            "representation_id": representation_id,
            "path": _relative_path(relative_path),
            "language": "binary",
            "size_bytes": size_bytes,
            "line_count": 0,
            "content": None,
            "previewable": False,
            "truncated": False,
            "reason": "非 UTF-8 二进制文件不提供站内文本预览。",
        }
    return {
        "representation_id": representation_id,
        "path": _relative_path(relative_path),
        "language": language_for_path(file_path),
        "size_bytes": size_bytes,
        "line_count": content.count("\n") + (0 if content.endswith("\n") else 1),
        "content": content,
        "previewable": True,
        "truncated": truncated,
            "reason": None,
    }
