from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from packages.ingestion import safe_tree_policy

COPY_BUFFER_SIZE = safe_tree_policy.COPY_BUFFER_SIZE
EXCLUDED_DIRECTORY_NAMES = safe_tree_policy.EXCLUDED_DIRECTORY_NAMES
WEIGHT_FILE_SUFFIXES = safe_tree_policy.WEIGHT_FILE_SUFFIXES
sha256_file = safe_tree_policy.sha256_file
_content_tree_digest = safe_tree_policy.content_tree_digest
_existing_destination_has_reparse = safe_tree_policy.existing_destination_has_reparse
_is_reparse_stat = safe_tree_policy.is_reparse_stat
_is_within = safe_tree_policy.is_within
_lstat = safe_tree_policy.lstat
_norm_path = safe_tree_policy.norm_path
_path_has_reparse = safe_tree_policy.path_has_reparse
_safety_reason = safe_tree_policy.safety_reason
_scan_directory = safe_tree_policy.scan_directory
_skip_record = safe_tree_policy.skip_record

SCHEMA_VERSION = 1
APPLY_CONFIRMATION = "COPY_FORMAL_ASSETS"
MAX_PARSE_BYTES = 16 * 1024 * 1024

CSS_REFERENCE_RE = re.compile(
    r"url\(\s*(['\"]?)(.*?)\1\s*\)|@import\s+(?:url\(\s*)?(['\"])(.*?)\3\s*\)?",
    re.IGNORECASE | re.DOTALL,
)
JS_REFERENCE_RE = re.compile(
    r"(?:import\s*(?:[^'\"]*?\s+from\s*)?|export\s+[^'\"]*?\s+from\s*|"
    r"import\s*\(|new\s+URL\s*\()\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


class CopyPlanError(RuntimeError):
    """Raised for malformed input or a safety-policy violation."""


class DependencyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str]] = []
        self._in_style = False
        self._style_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        tag = tag.lower()
        if "style" in values:
            self._style_chunks.append(values["style"])
        if tag == "style":
            self._in_style = True
            return
        if tag == "link" and "href" in values:
            rel = {part.casefold() for part in values.get("rel", "").split()}
            as_value = values.get("as", "").casefold()
            if rel & {"stylesheet", "icon"} or as_value in {"font", "image", "script", "style"}:
                self.references.append(("css" if "stylesheet" in rel else "asset", values["href"]))
        if tag == "script" and "src" in values:
            self.references.append(("js", values["src"]))
        if tag in {"img", "input", "audio", "video", "source"} and "src" in values:
            self.references.append(("asset", values["src"]))
        if tag == "video" and "poster" in values:
            self.references.append(("asset", values["poster"]))
        if tag in {"img", "source"} and "srcset" in values:
            for item in values["srcset"].split(","):
                candidate = item.strip().split(maxsplit=1)[0] if item.strip() else ""
                if candidate:
                    self.references.append(("asset", candidate))
        if tag in {"image", "use"} and "href" in values:
            self.references.append(("asset", values["href"]))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_chunks.append(data)

    @property
    def inline_css(self) -> str:
        return "\n".join(self._style_chunks)


def _safe_relative_path(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise CopyPlanError(f"invalid relative path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CopyPlanError(f"unsafe relative path: {raw!r}")
    if ":" in path.parts[0]:
        raise CopyPlanError(f"drive-qualified relative path: {raw!r}")
    return path


def _join_relative(root: Path, relative: PurePosixPath) -> Path:
    return root.joinpath(*relative.parts)


def _sanitize_remote(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    if "@" in value and ":" in value.split("@", 1)[1]:
        return value.split("@", 1)[1]
    return value.split("?", 1)[0].split("#", 1)[0]


def _git_evidence(directory: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "head": None, "remote_origin": None}
    git_metadata = directory / ".git"
    try:
        metadata_stat = _lstat(git_metadata)
    except OSError:
        result["reason"] = "no_local_git_directory"
        return result
    if _is_reparse_stat(metadata_stat) or not stat.S_ISDIR(metadata_stat.st_mode):
        result["reason"] = "git_metadata_not_safe_local_directory"
        return result
    git = shutil.which("git")
    if not git:
        result["reason"] = "git_not_found"
        return result
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})

    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                [git, "-c", "credential.helper=", "-C", str(directory), *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    head = run("rev-parse", "--verify", "HEAD")
    if head and re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        result["available"] = True
        result["head"] = head.lower()
    remote = run("config", "--get", "remote.origin.url")
    if remote:
        result["remote_origin"] = _sanitize_remote(remote)
    if not result["available"] and not remote:
        result["reason"] = "not_a_git_worktree_or_no_head"
    return result


def _read_text_for_dependencies(
    path: Path,
    source_root: Path,
    dependency_issues: list[dict[str, str]],
) -> str | None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        dependency_issues.append(
            _skip_record(source_root, path, f"stat_failed:{exc.__class__.__name__}")
        )
        return None
    if size > MAX_PARSE_BYTES:
        dependency_issues.append(_skip_record(source_root, path, "dependency_parse_size_limit"))
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        dependency_issues.append(
            _skip_record(source_root, path, f"read_failed:{exc.__class__.__name__}")
        )
        return None


def _css_references(text: str) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    for match in CSS_REFERENCE_RE.finditer(text):
        value = match.group(2) or match.group(4) or ""
        if value:
            references.append(("css" if match.group(4) else "asset", value.strip()))
    return references


def _js_references(text: str) -> list[tuple[str, str]]:
    return [("js", match.group(1).strip()) for match in JS_REFERENCE_RE.finditer(text)]


def _resolve_dependency(
    reference: str,
    base_file: Path,
    source_root: Path,
) -> tuple[Path | None, str | None]:
    value = html.unescape(reference).strip()
    if not value or "\x00" in value or value.startswith(("#", "//")):
        return None, "non_local_or_empty"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None, "external_url"
    decoded = unquote(parsed.path)
    if not decoded or "\\" in decoded:
        return None, "invalid_local_reference"
    if not decoded.startswith((".", "/")) and base_file.suffix.casefold() in {
        ".js",
        ".mjs",
        ".cjs",
    }:
        return None, "bare_js_module"
    candidate = (
        source_root.joinpath(*PurePosixPath(decoded.lstrip("/")).parts)
        if decoded.startswith("/")
        else base_file.parent.joinpath(*PurePosixPath(decoded).parts)
    )
    candidate = Path(os.path.abspath(candidate))
    if not _is_within(source_root, candidate):
        return None, "outside_source_root"
    try:
        relative = candidate.relative_to(Path(os.path.abspath(source_root)))
    except ValueError:
        return None, "outside_source_root"
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None, "unsafe_relative_dependency"
    reparse, reason = _path_has_reparse(source_root, candidate)
    if reparse:
        return None, reason
    try:
        value_stat = _lstat(candidate)
    except OSError as exc:
        return None, f"missing_or_unreadable:{exc.__class__.__name__}"
    if not stat.S_ISREG(value_stat.st_mode):
        return None, "dependency_not_regular_file"
    safety = _safety_reason(candidate, is_directory=False)
    if safety:
        return None, safety
    return candidate, None


def _discover_html_dependencies(
    html_path: Path,
    source_root: Path,
    dependency_issues: list[dict[str, str]],
) -> list[dict[str, Any]]:
    text = _read_text_for_dependencies(html_path, source_root, dependency_issues)
    if text is None:
        return []
    parser = DependencyParser()
    try:
        parser.feed(text)
    except Exception as exc:  # HTMLParser may surface malformed entity/pathological input.
        dependency_issues.append(
            _skip_record(source_root, html_path, f"html_parse_failed:{exc.__class__.__name__}")
        )
        return []
    queue: list[tuple[str, str, Path]] = [
        (kind, reference, html_path) for kind, reference in parser.references
    ]
    queue.extend(
        (kind, reference, html_path) for kind, reference in _css_references(parser.inline_css)
    )
    visited: set[str] = set()
    dependencies: list[dict[str, Any]] = []
    while queue:
        kind, reference, base_file = queue.pop(0)
        candidate, reason = _resolve_dependency(reference, base_file, source_root)
        if candidate is None:
            if reason not in {"external_url", "non_local_or_empty", "bare_js_module"}:
                dependency_issues.append(
                    {
                        "source_root": str(source_root),
                        "relative_path": base_file.relative_to(source_root).as_posix(),
                        "reference": reference,
                        "reason": reason or "dependency_rejected",
                    }
                )
            continue
        key = _norm_path(candidate)
        if key in visited:
            continue
        visited.add(key)
        try:
            digest, size = sha256_file(candidate)
        except OSError as exc:
            dependency_issues.append(
                _skip_record(source_root, candidate, f"hash_failed:{exc.__class__.__name__}")
            )
            continue
        relative = candidate.relative_to(Path(os.path.abspath(source_root))).as_posix()
        dependencies.append(
            {
                "kind": kind,
                "relative_path": relative,
                "source_path": str(candidate),
                "sha256": digest,
                "size_bytes": size,
                "referenced_by": base_file.relative_to(source_root).as_posix(),
            }
        )
        suffix = candidate.suffix.casefold()
        nested_text = None
        if suffix == ".css":
            nested_text = _read_text_for_dependencies(candidate, source_root, dependency_issues)
            if nested_text is not None:
                queue.extend(
                    (nested_kind, nested_ref, candidate)
                    for nested_kind, nested_ref in _css_references(nested_text)
                )
        elif suffix in {".js", ".mjs", ".cjs"}:
            nested_text = _read_text_for_dependencies(candidate, source_root, dependency_issues)
            if nested_text is not None:
                queue.extend(
                    (nested_kind, nested_ref, candidate)
                    for nested_kind, nested_ref in _js_references(nested_text)
                )
    return sorted(dependencies, key=lambda item: item["relative_path"].casefold())


def _destination_status(path: Path, expected_sha: str, expected_size: int) -> dict[str, Any]:
    try:
        value = _lstat(path)
    except FileNotFoundError:
        return {"status": "missing_would_copy"}
    except OSError as exc:
        return {"status": "unreadable", "reason": exc.__class__.__name__}
    if _is_reparse_stat(value):
        return {"status": "reparse_conflict"}
    if not stat.S_ISREG(value.st_mode):
        return {"status": "type_conflict"}
    try:
        digest, size = sha256_file(path)
    except OSError as exc:
        return {"status": "unreadable", "reason": exc.__class__.__name__}
    if digest == expected_sha and size == expected_size:
        return {"status": "already_present_identical", "sha256": digest, "size_bytes": size}
    return {"status": "content_conflict", "sha256": digest, "size_bytes": size}


def _add_operation(
    operations: dict[str, dict[str, Any]],
    operation: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    key = _norm_path(Path(operation["destination_path"]))
    existing = operations.get(key)
    if existing is None:
        operation["origins"] = [operation.pop("origin")]
        operations[key] = operation
        return
    if (
        existing["sha256"] != operation["sha256"]
        or existing["source_path"] != operation["source_path"]
    ):
        errors.append(
            {
                "reason": "destination_collision",
                "destination_path": operation["destination_path"],
                "first_source": existing["source_path"],
                "second_source": operation["source_path"],
            }
        )
        return
    origin = operation["origin"]
    if origin not in existing["origins"]:
        existing["origins"].append(origin)


def _verify_directory_destination(
    destination: Path,
    expected_files: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        value = _lstat(destination)
    except FileNotFoundError:
        return {"status": "missing_would_create", "tree_digest": None}
    except OSError as exc:
        return {"status": "unreadable", "reason": exc.__class__.__name__, "tree_digest": None}
    if _is_reparse_stat(value):
        return {"status": "reparse_conflict", "tree_digest": None}
    if not stat.S_ISDIR(value.st_mode):
        return {"status": "type_conflict", "tree_digest": None}
    expected_names = {item["relative_path"] for item in expected_files}
    actual: list[dict[str, Any]] = []
    unexpected: list[str] = []
    stack = [destination]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            return {"status": "unreadable", "reason": exc.__class__.__name__, "tree_digest": None}
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(destination).as_posix()
            try:
                item_stat = entry.stat(follow_symlinks=False)
            except OSError:
                unexpected.append(relative)
                continue
            if _is_reparse_stat(item_stat):
                unexpected.append(relative)
            elif stat.S_ISDIR(item_stat.st_mode):
                stack.append(path)
            elif stat.S_ISREG(item_stat.st_mode):
                if relative not in expected_names:
                    unexpected.append(relative)
                    continue
                digest, size = sha256_file(path)
                actual.append({"relative_path": relative, "sha256": digest, "size_bytes": size})
            else:
                unexpected.append(relative)
    expected_digest = _content_tree_digest(expected_files)
    actual_digest = _content_tree_digest(actual)
    if unexpected:
        status = "unexpected_destination_entries"
    elif len(actual) == len(expected_files) and actual_digest == expected_digest:
        status = "already_present_identical"
    else:
        status = "incomplete_or_conflicting"
    return {
        "status": status,
        "tree_digest": actual_digest,
        "expected_tree_digest": expected_digest,
        "unexpected_entries": sorted(unexpected),
    }


def build_plan(preflight_path: Path, core_root: Path) -> dict[str, Any]:
    preflight_path = Path(os.path.abspath(preflight_path))
    core_root = Path(os.path.abspath(core_root))
    preflight_sha, preflight_size = sha256_file(preflight_path)
    payload = json.loads(preflight_path.read_text(encoding="utf-8"))
    if payload.get("report_kind") != "external-root-relocation-preflight":
        raise CopyPlanError("unexpected preflight report_kind")
    mappings_raw = payload.get("root_mappings")
    formal_targets = payload.get("formal_targets")
    if not isinstance(mappings_raw, list) or not isinstance(formal_targets, list):
        raise CopyPlanError("preflight must contain root_mappings and formal_targets arrays")
    declared_count = payload.get("scope", {}).get("unique_formal_targets")
    if declared_count != len(formal_targets):
        mismatch = f"declared={declared_count!r} actual={len(formal_targets)}"
        raise CopyPlanError(f"formal target count mismatch: {mismatch}")

    mappings: dict[str, dict[str, Any]] = {}
    root_reports: list[dict[str, Any]] = []
    try:
        core_stat = _lstat(core_root)
    except OSError as exc:
        raise CopyPlanError(f"core root unavailable: {exc.__class__.__name__}") from exc
    if _is_reparse_stat(core_stat) or not stat.S_ISDIR(core_stat.st_mode):
        raise CopyPlanError("core root must be a real directory, not a reparse point")
    for raw in mappings_raw:
        source_root = Path(os.path.abspath(raw["source_root"]))
        destination_root = Path(os.path.abspath(raw["planned_core_root"]))
        if not _is_within(core_root, destination_root):
            raise CopyPlanError(f"planned destination is outside core root: {destination_root}")
        destination_reparse, destination_reason = _existing_destination_has_reparse(
            core_root, destination_root
        )
        if destination_reparse:
            raise CopyPlanError(
                f"unsafe planned destination ancestry: {destination_root}: {destination_reason}"
            )
        source_key = _norm_path(source_root)
        if source_key in mappings:
            raise CopyPlanError(f"duplicate source root mapping: {source_root}")
        try:
            root_stat = _lstat(source_root)
        except OSError as exc:
            raise CopyPlanError(
                f"source root unavailable: {source_root}: {exc.__class__.__name__}"
            ) from exc
        if _is_reparse_stat(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise CopyPlanError(f"source root is not a real directory: {source_root}")
        mappings[source_key] = {
            "source_root": source_root,
            "destination_root": destination_root,
            "raw": raw,
        }
        root_reports.append(
            {
                "source_root": str(source_root),
                "planned_core_root": str(destination_root),
                "source_root_reparse": False,
                "destination_within_core": True,
            }
        )

    errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    dependency_issues: list[dict[str, str]] = []
    dependencies: list[dict[str, Any]] = []
    directory_reports: list[dict[str, Any]] = []
    target_reports: list[dict[str, Any]] = []
    operations: dict[str, dict[str, Any]] = {}
    formal_html: list[tuple[Path, Path, Path]] = []

    seen_targets: set[tuple[str, str]] = set()
    for index, target in enumerate(formal_targets):
        try:
            source_root = Path(os.path.abspath(target["source_root"]))
            mapping = mappings[_norm_path(source_root)]
            relative = _safe_relative_path(target["relative_path"])
        except (KeyError, CopyPlanError) as exc:
            errors.append(
                {"reason": "invalid_formal_target", "index": str(index), "detail": str(exc)}
            )
            continue
        target_key = (_norm_path(source_root), relative.as_posix().casefold())
        if target_key in seen_targets:
            errors.append(
                {
                    "reason": "duplicate_formal_target",
                    "source_root": str(source_root),
                    "relative_path": relative.as_posix(),
                }
            )
            continue
        seen_targets.add(target_key)
        source_path = _join_relative(source_root, relative)
        destination_path = _join_relative(mapping["destination_root"], relative)
        declared_destination = Path(os.path.abspath(target["planned_core_path"]))
        if _norm_path(destination_path) != _norm_path(declared_destination):
            errors.append(
                {
                    "reason": "planned_core_path_mismatch",
                    "relative_path": relative.as_posix(),
                    "computed": str(destination_path),
                    "declared": str(declared_destination),
                }
            )
            continue
        reparse, reason = _path_has_reparse(source_root, source_path)
        if reparse:
            errors.append(
                _skip_record(source_root, source_path, reason or "reparse_or_unavailable")
            )
            continue
        kind = target.get("kind")
        safety = _safety_reason(source_path, is_directory=kind == "directory")
        if safety:
            skipped.append(_skip_record(source_root, source_path, safety))
            errors.append(
                _skip_record(source_root, source_path, "formal_target_rejected_by_policy")
            )
            continue
        base_report: dict[str, Any] = {
            "source_root": str(source_root),
            "relative_path": relative.as_posix(),
            "source_path": str(source_path),
            "destination_path": str(destination_path),
            "kind": kind,
            "references": target.get("references", []),
        }
        if kind == "file":
            try:
                value = _lstat(source_path)
                if not stat.S_ISREG(value.st_mode):
                    raise CopyPlanError("formal file target is not a regular file")
                digest, size = sha256_file(source_path)
            except (OSError, CopyPlanError) as exc:
                errors.append(
                    _skip_record(
                        source_root, source_path, f"formal_file_failed:{exc.__class__.__name__}"
                    )
                )
                continue
            base_report.update({"sha256": digest, "size_bytes": size})
            if digest != target.get("sha256") or size != target.get("size_bytes"):
                errors.append(
                    {
                        "reason": "preflight_file_fingerprint_changed",
                        "source_root": str(source_root),
                        "relative_path": relative.as_posix(),
                    }
                )
            destination_status = _destination_status(destination_path, digest, size)
            base_report["destination"] = destination_status
            if destination_status["status"] not in {
                "missing_would_copy",
                "already_present_identical",
            }:
                errors.append(
                    {
                        "reason": "destination_conflict",
                        "destination_path": str(destination_path),
                        "status": destination_status["status"],
                    }
                )
            _add_operation(
                operations,
                {
                    "source_root": str(source_root),
                    "source_relative_path": relative.as_posix(),
                    "source_path": str(source_path),
                    "destination_path": str(destination_path),
                    "sha256": digest,
                    "size_bytes": size,
                    "origin": "formal_file",
                },
                errors,
            )
            if source_path.suffix.casefold() in {".html", ".htm"}:
                formal_html.append((source_path, source_root, mapping["destination_root"]))
        elif kind == "directory":
            try:
                value = _lstat(source_path)
                if not stat.S_ISDIR(value.st_mode):
                    raise CopyPlanError("formal directory target is not a directory")
            except (OSError, CopyPlanError) as exc:
                errors.append(
                    _skip_record(
                        source_root,
                        source_path,
                        f"formal_directory_failed:{exc.__class__.__name__}",
                    )
                )
                continue
            files = _scan_directory(source_path, source_root, skipped, errors)
            for item in files:
                destination_file = mapping["destination_root"].joinpath(
                    *PurePosixPath(item["source_relative_path"]).parts
                )
                status_item = _destination_status(
                    destination_file, item["sha256"], item["size_bytes"]
                )
                if status_item["status"] not in {"missing_would_copy", "already_present_identical"}:
                    errors.append(
                        {
                            "reason": "destination_conflict",
                            "destination_path": str(destination_file),
                            "status": status_item["status"],
                        }
                    )
                _add_operation(
                    operations,
                    {
                        "source_root": str(source_root),
                        "source_relative_path": item["source_relative_path"],
                        "source_path": item["source_path"],
                        "destination_path": str(destination_file),
                        "sha256": item["sha256"],
                        "size_bytes": item["size_bytes"],
                        "origin": f"formal_directory:{relative.as_posix()}",
                    },
                    errors,
                )
            destination_tree = _verify_directory_destination(destination_path, files)
            if destination_tree["status"] not in {
                "missing_would_create",
                "already_present_identical",
                "incomplete_or_conflicting",
            }:
                errors.append(
                    {
                        "reason": "directory_destination_conflict",
                        "destination_path": str(destination_path),
                        "status": destination_tree["status"],
                    }
                )
            directory_report = {
                **base_report,
                "preflight_tree_digest": target.get("tree_digest"),
                "preflight_file_count": target.get("file_count"),
                "safe_file_count": len(files),
                "safe_aggregate_size_bytes": sum(item["size_bytes"] for item in files),
                "content_tree_digest": _content_tree_digest(files),
                "file_ledger": files,
                "git": _git_evidence(source_path),
                "destination": destination_tree,
            }
            directory_reports.append(directory_report)
            base_report = {
                key: value for key, value in directory_report.items() if key != "file_ledger"
            }
        else:
            errors.append(
                {
                    "reason": "unknown_formal_target_kind",
                    "relative_path": relative.as_posix(),
                    "kind": str(kind),
                }
            )
            continue
        target_reports.append(base_report)

    for html_path, source_root, destination_root in formal_html:
        discovered = _discover_html_dependencies(html_path, source_root, dependency_issues)
        for item in discovered:
            destination_path = destination_root.joinpath(
                *PurePosixPath(item["relative_path"]).parts
            )
            dependency = {
                **item,
                "destination_path": str(destination_path),
                "html_source": str(html_path),
            }
            dependencies.append(dependency)
            destination_status = _destination_status(
                destination_path, item["sha256"], item["size_bytes"]
            )
            dependency["destination"] = destination_status
            if destination_status["status"] not in {
                "missing_would_copy",
                "already_present_identical",
            }:
                errors.append(
                    {
                        "reason": "dependency_destination_conflict",
                        "destination_path": str(destination_path),
                        "status": destination_status["status"],
                    }
                )
            _add_operation(
                operations,
                {
                    "source_root": str(source_root),
                    "source_relative_path": item["relative_path"],
                    "source_path": item["source_path"],
                    "destination_path": str(destination_path),
                    "sha256": item["sha256"],
                    "size_bytes": item["size_bytes"],
                    "origin": f"html_dependency:{html_path.relative_to(source_root).as_posix()}",
                },
                errors,
            )

    operation_rows = sorted(operations.values(), key=lambda row: row["destination_path"].casefold())
    for operation in operation_rows:
        operation["destination"] = _destination_status(
            Path(operation["destination_path"]), operation["sha256"], operation["size_bytes"]
        )
    copy_count = sum(
        1 for row in operation_rows if row["destination"]["status"] == "missing_would_copy"
    )
    identical_count = sum(
        1 for row in operation_rows if row["destination"]["status"] == "already_present_identical"
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "external-formal-asset-copy-plan",
        "mode": "dry-run",
        "input": {
            "preflight_path": str(preflight_path),
            "preflight_sha256": preflight_sha,
            "preflight_size_bytes": preflight_size,
            "declared_formal_targets": declared_count,
        },
        "core_root": str(core_root),
        "policy": {
            "follows_reparse_points": False,
            "overwrites_destinations": False,
            "excluded_directories": sorted(EXCLUDED_DIRECTORY_NAMES),
            "sensitive_files_are_opened_or_hashed": False,
            "weight_suffixes": sorted(WEIGHT_FILE_SUFFIXES),
            "directory_digest_method": (
                "SHA-256 over sorted relative_path\\0size\\0file_sha256 records"
            ),
            "html_dependency_policy": (
                "local-only, within mapped source root, non-reparse, policy-allowed"
            ),
        },
        "root_mappings": root_reports,
        "formal_targets": target_reports,
        "formal_directories": directory_reports,
        "html_dependencies": dependencies,
        "dependency_issues": dependency_issues,
        "skipped_without_content_read": skipped,
        "copy_ledger": operation_rows,
        "errors": errors,
        "summary": {
            "formal_target_count": len(target_reports),
            "formal_directory_count": len(directory_reports),
            "html_dependency_count": len(dependencies),
            "unique_file_operation_count": len(operation_rows),
            "would_copy_file_count": copy_count,
            "already_present_identical_count": identical_count,
            "copy_bytes": sum(
                row["size_bytes"]
                for row in operation_rows
                if row["destination"]["status"] == "missing_would_copy"
            ),
            "skipped_count": len(skipped),
            "dependency_issue_count": len(dependency_issues),
            "error_count": len(errors),
        },
        "non_mutation_attestation": {
            "source_writes": 0,
            "destination_writes": 0,
            "junction_changes": 0,
            "configuration_changes": 0,
            "note": (
                "build_plan is read-only; only the explicit report writer may create report files."
            ),
        },
    }
    return report


def _exclusive_copy(operation: dict[str, Any], core_root: Path) -> str:
    source = Path(operation["source_path"])
    destination = Path(operation["destination_path"])
    current = _destination_status(destination, operation["sha256"], operation["size_bytes"])
    if current["status"] == "already_present_identical":
        return "already_present_identical"
    if current["status"] != "missing_would_copy":
        raise CopyPlanError(f"refusing to overwrite destination: {destination}")
    reparse, reason = _existing_destination_has_reparse(core_root, destination.parent)
    if reparse:
        raise CopyPlanError(f"unsafe destination ancestry: {destination}: {reason}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    reparse, reason = _existing_destination_has_reparse(core_root, destination.parent)
    if reparse:
        raise CopyPlanError(f"unsafe destination ancestry after mkdir: {destination}: {reason}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(destination, flags, 0o644)
        created = True
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as destination_handle:
            descriptor = None
            while chunk := source_handle.read(COPY_BUFFER_SIZE):
                destination_handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        if digest.hexdigest() != operation["sha256"] or total != operation["size_bytes"]:
            raise CopyPlanError(f"source changed while copying: {source}")
        verified = _destination_status(destination, operation["sha256"], operation["size_bytes"])
        if verified["status"] != "already_present_identical":
            raise CopyPlanError(f"destination verification failed: {destination}")
        return "copied_and_verified"
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def apply_plan(report: dict[str, Any], confirmation: str) -> dict[str, Any]:
    if confirmation != APPLY_CONFIRMATION:
        raise CopyPlanError(f"--confirm-apply must equal {APPLY_CONFIRMATION!r}")
    if report["errors"]:
        raise CopyPlanError("refusing apply because dry-run contains errors")
    results: list[dict[str, str]] = []
    core_root = Path(report["core_root"])
    for operation in report["copy_ledger"]:
        try:
            outcome = _exclusive_copy(operation, core_root)
        except (CopyPlanError, OSError) as exc:
            results.append(
                {
                    "destination_path": operation["destination_path"],
                    "outcome": "error",
                    "reason": f"{exc.__class__.__name__}:{exc}",
                }
            )
            report["errors"].append(
                {
                    "reason": "apply_copy_failed",
                    "destination_path": operation["destination_path"],
                }
            )
            break
        results.append({"destination_path": operation["destination_path"], "outcome": outcome})
    file_verification: list[dict[str, Any]] = []
    for operation in report["copy_ledger"]:
        status = _destination_status(
            Path(operation["destination_path"]),
            operation["sha256"],
            operation["size_bytes"],
        )
        file_verification.append(
            {"destination_path": operation["destination_path"], "status": status["status"]}
        )
        if status["status"] != "already_present_identical":
            report["errors"].append(
                {
                    "reason": "apply_file_verification_failed",
                    "destination_path": operation["destination_path"],
                    "status": status["status"],
                }
            )
    directory_verification: list[dict[str, Any]] = []
    for directory in report["formal_directories"]:
        status = _verify_directory_destination(
            Path(directory["destination_path"]), directory["file_ledger"]
        )
        directory_verification.append({"destination_path": directory["destination_path"], **status})
        if status["status"] != "already_present_identical":
            report["errors"].append(
                {
                    "reason": "apply_directory_verification_failed",
                    "destination_path": directory["destination_path"],
                    "status": status["status"],
                }
            )
    report["mode"] = "apply"
    report["apply_results"] = results
    report["apply_file_verification"] = file_verification
    report["apply_directory_verification"] = directory_verification
    report["summary"]["copied_and_verified_count"] = sum(
        1 for result in results if result["outcome"] == "copied_and_verified"
    )
    report["summary"]["apply_identical_skip_count"] = sum(
        1 for result in results if result["outcome"] == "already_present_identical"
    )
    report["non_mutation_attestation"] = {
        "source_writes": 0,
        "junction_changes": 0,
        "configuration_changes": 0,
        "destination_file_creations": report["summary"]["copied_and_verified_count"],
        "destination_overwrites": 0,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# External formal asset copy report",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Preflight: `{report['input']['preflight_path']}`",
        f"- Preflight SHA-256: `{report['input']['preflight_sha256']}`",
        f"- Core root: `{report['core_root']}`",
        f"- Formal targets reviewed: {summary['formal_target_count']}",
        f"- Recursive formal directories: {summary['formal_directory_count']}",
        f"- Local HTML dependencies allowlisted: {summary['html_dependency_count']}",
        f"- Unique safe files: {summary['unique_file_operation_count']}",
        f"- Files that would be copied: {summary['would_copy_file_count']}",
        f"- Bytes that would be copied: {summary['copy_bytes']}",
        "- Sensitive/excluded/reparse entries skipped without content read: "
        f"{summary['skipped_count']}",
        f"- Dependency issues: {summary['dependency_issue_count']}",
        f"- Errors: {summary['error_count']}",
        "",
        "## Safety result",
        "",
        "The source roots, registered source configuration, and junctions were not changed. "
        "The copy ledger never follows reparse points and never overwrites a destination. "
        "Sensitive-name entries are reported by relative path only and are not opened or hashed.",
        "",
        "## Root mappings",
        "",
    ]
    for mapping in report["root_mappings"]:
        lines.append(f"- `{mapping['source_root']}` → `{mapping['planned_core_root']}`")
    lines.extend(["", "## Skipped entries", ""])
    if report["skipped_without_content_read"]:
        for item in report["skipped_without_content_read"]:
            lines.append(f"- `{item['relative_path']}` — {item['reason']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Errors", ""])
    if report["errors"]:
        for item in report["errors"]:
            detail = (
                item.get("relative_path") or item.get("destination_path") or item.get("detail", "")
            )
            lines.append(f"- `{item.get('reason', 'error')}` — `{detail}`")
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def _write_reports(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path = Path(os.path.abspath(json_path))
    markdown_path = Path(os.path.abspath(markdown_path))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or explicitly apply a non-overwriting copy of preflight formal assets."
    )
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Create missing target files; default is dry-run."
    )
    parser.add_argument("--confirm-apply", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_plan(args.preflight, args.core_root)
        if args.apply:
            report = apply_plan(report, args.confirm_apply)
        _write_reports(report, args.report_json, args.report_md)
    except (CopyPlanError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"external_asset_copy: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
