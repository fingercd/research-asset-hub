"""Shared safe-directory scan policy for external copies and source retirement.

The scanner deliberately decides whether a name is safe *before* opening a
file.  Reparse points are never followed.  Its content digest excludes mutable
metadata such as mtime so a verified copy can be compared with its predecessor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

COPY_BUFFER_SIZE = 1024 * 1024
SAFE_TREE_POLICY_VERSION = "external-asset-copy-safe-tree-v1"

EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "browser-profile",
    "browser-profiles",
    "build",
    "cache",
    "caches",
    "checkpoint",
    "checkpoints",
    "chrome-profile",
    "chromium-profile",
    "coverage",
    "data",
    "dataset",
    "datasets",
    "dist",
    "env",
    "generated-samples",
    "generated_samples",
    "model-weights",
    "model_weights",
    "node_modules",
    "out",
    "playwright-profile",
    "samples-generated",
    "site-packages",
    "target",
    "training-data",
    "training_data",
    "user data",
    "user-data",
    "user_data",
    "vendor",
    "venv",
    "weight",
    "weights",
}

WEIGHT_FILE_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".gguf",
    ".h5",
    ".onnx",
    ".pb",
    ".pt",
    ".pth",
    ".safetensors",
    ".tflite",
}

SENSITIVE_SUFFIXES = {
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".kubeconfig",
    ".p12",
    ".pem",
    ".pfx",
    ".ppk",
}

SENSITIVE_EXACT_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".sandbox-secrets",
    "auth.json",
    "authorization.json",
    "cookie.json",
    "cookies.json",
    "credential.json",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "passwd",
    "password",
    "secret.json",
    "secrets.json",
    "token.json",
    "tokens.json",
}

SENSITIVE_TOKEN_RE = re.compile(
    r"(?:^|[._-])(?:api[-_]?key|access[-_]?key|auth|authorization|credential|credentials|"
    r"private[-_]?key|secret|secrets|password|passwd|cookie|cookies)(?:$|[._-])",
    re.IGNORECASE,
)

ALLOWED_EXCLUSION_REASONS = frozenset(
    {"excluded_directory", "excluded_weight_or_checkpoint", "sensitive_name"}
)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_BUFFER_SIZE):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def norm_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([norm_path(root), norm_path(candidate)]) == norm_path(
            root
        )
    except ValueError:
        return False


def is_reparse_stat(value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def lstat(path: Path) -> os.stat_result:
    return os.lstat(path)


def path_has_reparse(root: Path, candidate: Path) -> tuple[bool, str | None]:
    if not is_within(root, candidate):
        return True, "outside_source_root"
    root_abs = Path(os.path.abspath(root))
    candidate_abs = Path(os.path.abspath(candidate))
    try:
        relative = candidate_abs.relative_to(root_abs)
    except ValueError:
        return True, "outside_source_root"
    current = root_abs
    for part in (".", *relative.parts):
        if part != ".":
            current /= part
        try:
            value = lstat(current)
        except OSError as exc:
            return True, f"lstat_failed:{exc.__class__.__name__}"
        if is_reparse_stat(value):
            return True, "reparse_point"
    return False, None


def existing_destination_has_reparse(
    root: Path, candidate: Path
) -> tuple[bool, str | None]:
    """Inspect only existing destination ancestors without resolving a link."""

    if not is_within(root, candidate):
        return True, "outside_core_root"
    root_abs = Path(os.path.abspath(root))
    candidate_abs = Path(os.path.abspath(candidate))
    try:
        relative = candidate_abs.relative_to(root_abs)
    except ValueError:
        return True, "outside_core_root"
    current = root_abs
    for part in (".", *relative.parts):
        if part != ".":
            current /= part
        try:
            value = lstat(current)
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            return True, f"destination_lstat_failed:{exc.__class__.__name__}"
        if is_reparse_stat(value):
            return True, "destination_reparse_point"
        if part != "." and not stat.S_ISDIR(value.st_mode) and current != candidate_abs:
            return True, "destination_parent_not_directory"
    return False, None


def safety_reason(path: Path, *, is_directory: bool) -> str | None:
    name = path.name.casefold()
    if name in SENSITIVE_EXACT_NAMES or name.startswith(".env."):
        return "sensitive_name"
    if name in {"auth", "credentials", "keys", "secrets", "tokens"}:
        return "sensitive_name"
    if SENSITIVE_TOKEN_RE.search(name) or path.suffix.casefold() in SENSITIVE_SUFFIXES:
        return "sensitive_name"
    if is_directory and name in EXCLUDED_DIRECTORY_NAMES:
        return "excluded_directory"
    if not is_directory and path.suffix.casefold() in WEIGHT_FILE_SUFFIXES:
        return "excluded_weight_or_checkpoint"
    return None


def content_tree_digest(files: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda row: row["relative_path"].casefold()):
        line = f"{item['relative_path']}\0{item['size_bytes']}\0{item['sha256']}\n"
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def skip_record(source_root: Path, path: Path, reason: str) -> dict[str, str]:
    try:
        relative = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(source_root)))
        display = relative.as_posix()
    except ValueError:
        display = path.name
    return {"source_root": str(source_root), "relative_path": display, "reason": reason}


def scan_directory(
    directory: Path,
    source_root: Path,
    skipped: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(
                os.scandir(current), key=lambda entry: entry.name.casefold(), reverse=True
            )
        except OSError as exc:
            errors.append(
                skip_record(source_root, current, f"scandir_failed:{exc.__class__.__name__}")
            )
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append(
                    skip_record(source_root, path, f"lstat_failed:{exc.__class__.__name__}")
                )
                continue
            if is_reparse_stat(entry_stat):
                skipped.append(skip_record(source_root, path, "reparse_point"))
                continue
            is_directory = stat.S_ISDIR(entry_stat.st_mode)
            reason = safety_reason(path, is_directory=is_directory)
            if reason:
                skipped.append(skip_record(source_root, path, reason))
                continue
            if is_directory:
                stack.append(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                skipped.append(skip_record(source_root, path, "special_file"))
                continue
            try:
                digest, size = sha256_file(path)
            except OSError as exc:
                errors.append(
                    skip_record(source_root, path, f"hash_failed:{exc.__class__.__name__}")
                )
                continue
            relative_source = Path(os.path.abspath(path)).relative_to(
                Path(os.path.abspath(source_root))
            )
            relative_target = Path(os.path.abspath(path)).relative_to(
                Path(os.path.abspath(directory))
            )
            ledger.append(
                {
                    "relative_path": relative_target.as_posix(),
                    "source_relative_path": relative_source.as_posix(),
                    "source_path": str(path),
                    "sha256": digest,
                    "size_bytes": size,
                }
            )
    return sorted(ledger, key=lambda row: row["relative_path"].casefold())


def policy_sha256() -> str:
    payload = {
        "policy_version": SAFE_TREE_POLICY_VERSION,
        "excluded_directories": sorted(EXCLUDED_DIRECTORY_NAMES),
        "sensitive_exact_names": sorted(SENSITIVE_EXACT_NAMES),
        "sensitive_suffixes": sorted(SENSITIVE_SUFFIXES),
        "sensitive_token_pattern": SENSITIVE_TOKEN_RE.pattern,
        "weight_suffixes": sorted(WEIGHT_FILE_SUFFIXES),
        "digest_method": "SHA-256 over sorted relative_path\\0size\\0file_sha256 records",
        "follows_reparse_points": False,
        "sensitive_files_are_opened_or_hashed": False,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
