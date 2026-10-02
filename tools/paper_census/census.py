#!/usr/bin/env python3
"""Build a resumable, read-only census of paper-library representations.

The scanner never writes below a scanned root. It discovers PDF, HTML, catalog,
and manifest files; hashes them; and inspects PDF structure. All writes are
limited to the explicitly supplied output prefix and checkpoint path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - exercised only outside the project environment
    PdfReader = None  # type: ignore[assignment,misc]


SCHEMA_VERSION = "paper-census/v1"
CHECKPOINT_SCHEMA_VERSION = "paper-census-checkpoint/v1"
HASH_ALGORITHM = "sha256"
DEFAULT_HASH_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_CHECKPOINT_EVERY = 25

# Matching is component-based and case-insensitive. It intentionally does not
# exclude a path merely because words such as "cache" occur in a paper title.
DEFAULT_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".cache",
        "cache",
        "caches",
        "weights",
        "model_weights",
        "checkpoints",
        "recovery",
        "_recovery",
        "recovery-rehearsal",
        "recovery_rehearsal",
        "restore-rehearsal",
        "restore_rehearsal",
    }
)

# These are never discovered, hashed, or listed. This is deliberately an exact
# filename policy so a legitimate paper about authentication is not suppressed.
SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".sandbox-secrets",
        "auth.json",
        "credentials.json",
        "credential.json",
        "token.json",
        "tokens.json",
        "secrets.json",
    }
)
STRUCTURED_SUFFIXES = frozenset({".json", ".jsonl", ".yaml", ".yml", ".csv", ".tsv", ".md"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RootSpec:
    root_id: str
    path: str
    label: str


@dataclass(frozen=True)
class Candidate:
    record_id: str
    root_id: str
    root_path: str
    relative_path: str
    absolute_path: str
    kind: str

    @property
    def shard_key(self) -> bytes:
        return f"{self.root_id}\0{self.relative_path}".encode("utf-8", errors="surrogatepass")


@dataclass
class DiscoveryStats:
    directories_excluded: int = 0
    reparse_directories_skipped: int = 0
    reparse_files_skipped: int = 0
    sensitive_files_skipped: int = 0
    unsupported_files_skipped: int = 0


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_prefix(path: Path) -> Path:
    if path.suffix.casefold() in {".json", ".csv", ".md"}:
        return path.with_suffix("")
    return path


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding=encoding, newline="")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    _atomic_write_text(path, text)


def _is_sensitive_filename(name: str) -> bool:
    folded = name.casefold()
    return (
        folded in SENSITIVE_FILE_NAMES
        or folded.startswith(".env.")
        or folded.endswith((".pem", ".p12", ".pfx", ".key"))
    )


def classify_file(path: Path) -> str | None:
    """Return the representation kind without opening the file."""

    if _is_sensitive_filename(path.name):
        return None
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix not in STRUCTURED_SUFFIXES:
        return None
    folded = path.name.casefold()
    if "manifest" in folded:
        return "manifest"
    if "catalog" in folded:
        return "catalog"
    return None


def _file_attributes(path: Path) -> int:
    try:
        return int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return 0


def _is_reparse_point(path: Path) -> bool:
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(_file_attributes(path) & reparse_flag)


def _path_is_within(path: Path, parent: Path | None) -> bool | None:
    if parent is None:
        return None
    try:
        path_norm = os.path.normcase(os.path.abspath(path))
        parent_norm = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath((path_norm, parent_norm)) == parent_norm
    except ValueError:
        return False


def _make_root_spec(path: Path) -> RootSpec:
    resolved = path.resolve(strict=True)
    normalized = os.path.normcase(str(resolved))
    root_id = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    label = resolved.name or resolved.anchor.rstrip("\\/") or root_id
    return RootSpec(root_id=root_id, path=str(resolved), label=label)


def _make_candidate(root: RootSpec, absolute_path: Path, kind: str) -> Candidate:
    root_path = Path(root.path)
    if root_path.is_file():
        relative = absolute_path.name
    else:
        relative = absolute_path.relative_to(root_path).as_posix()
    raw_id = f"{root.root_id}\0{relative}".encode("utf-8", errors="surrogatepass")
    record_id = f"file-{hashlib.sha256(raw_id).hexdigest()}"
    return Candidate(
        record_id=record_id,
        root_id=root.root_id,
        root_path=root.path,
        relative_path=relative,
        absolute_path=str(absolute_path),
        kind=kind,
    )


def discover_candidates(
    roots: Sequence[RootSpec],
    excluded_dir_names: frozenset[str],
) -> tuple[list[Candidate], DiscoveryStats, list[dict[str, str]]]:
    """Discover supported files without following symlinks or junctions."""

    candidates: list[Candidate] = []
    stats = DiscoveryStats()
    errors: list[dict[str, str]] = []
    excluded = {name.casefold() for name in excluded_dir_names}

    for root in roots:
        root_path = Path(root.path)
        if root_path.is_file():
            if _is_reparse_point(root_path):
                stats.reparse_files_skipped += 1
                continue
            kind = classify_file(root_path)
            if kind is not None:
                candidates.append(_make_candidate(root, root_path, kind))
            elif _is_sensitive_filename(root_path.name):
                stats.sensitive_files_skipped += 1
            else:
                stats.unsupported_files_skipped += 1
            continue

        def onerror(error: OSError, *, root_value: str = root.path) -> None:
            errors.append(
                {
                    "stage": "discover",
                    "root": root_value,
                    "path": str(getattr(error, "filename", "") or ""),
                    "error": f"{type(error).__name__}: {error}",
                }
            )

        for current, dirnames, filenames in os.walk(
            root_path, topdown=True, onerror=onerror, followlinks=False
        ):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames, key=str.casefold):
                directory = current_path / dirname
                if dirname.casefold() in excluded:
                    stats.directories_excluded += 1
                elif _is_reparse_point(directory):
                    stats.reparse_directories_skipped += 1
                else:
                    kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in sorted(filenames, key=str.casefold):
                file_path = current_path / filename
                if _is_sensitive_filename(filename):
                    stats.sensitive_files_skipped += 1
                    continue
                kind = classify_file(file_path)
                if kind is None:
                    stats.unsupported_files_skipped += 1
                    continue
                # Most library trees contain hundreds of thousands of source,
                # dependency, and repository files that are not census
                # candidates.  Filter those names before the comparatively
                # expensive Windows lstat/reparse query; supported candidates
                # still receive the same no-follow safety check.
                if _is_reparse_point(file_path):
                    stats.reparse_files_skipped += 1
                    continue
                candidates.append(_make_candidate(root, file_path, kind))

    candidates.sort(key=lambda item: (item.root_id, item.relative_path.casefold(), item.relative_path))
    return candidates, stats, errors


def _candidate_shard(candidate: Candidate, shard_count: int) -> int:
    digest = hashlib.sha256(candidate.shard_key).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) % shard_count


def _hash_file(path: Path, chunk_size: int = DEFAULT_HASH_CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_pdf_metadata(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def inspect_pdf(path: Path) -> dict[str, Any]:
    """Inspect PDF magic, encryption, page count, and minimal embedded metadata."""

    inspection: dict[str, Any] = {
        "magic_ok": False,
        "magic_offset": None,
        "parse_status": "not_attempted",
        "encrypted": None,
        "page_count": None,
        "metadata_title": None,
        "metadata_author": None,
        "parser": "pypdf" if PdfReader is not None else None,
        "error": None,
    }
    try:
        with path.open("rb") as handle:
            prefix = handle.read(1024)
        magic_offset = prefix.find(b"%PDF-")
        inspection["magic_offset"] = magic_offset if magic_offset >= 0 else None
        inspection["magic_ok"] = 0 <= magic_offset <= 1023
    except OSError as error:
        inspection["parse_status"] = "read_error"
        inspection["error"] = f"{type(error).__name__}: {error}"
        return inspection

    if PdfReader is None:
        inspection["parse_status"] = "pypdf_unavailable"
        inspection["error"] = "pypdf is not installed"
        return inspection

    try:
        reader = PdfReader(str(path), strict=False)
        encrypted = bool(reader.is_encrypted)
        inspection["encrypted"] = encrypted
        if encrypted:
            try:
                unlocked = bool(reader.decrypt(""))
            except Exception:  # pypdf exposes several backend-specific errors
                unlocked = False
            if not unlocked:
                inspection["parse_status"] = "encrypted_locked"
                return inspection
            inspection["parse_status"] = "encrypted_empty_password"
        else:
            inspection["parse_status"] = "ok"

        inspection["page_count"] = len(reader.pages)
        try:
            metadata = reader.metadata
            if metadata is not None:
                inspection["metadata_title"] = _safe_pdf_metadata(metadata.title)
                inspection["metadata_author"] = _safe_pdf_metadata(metadata.author)
        except Exception as error:  # metadata corruption must not hide valid pages
            inspection["metadata_error"] = f"{type(error).__name__}: {error}"
    except Exception as error:
        inspection["parse_status"] = "parse_error"
        inspection["error"] = f"{type(error).__name__}: {error}"
    return inspection


def _load_checkpoint(path: Path, resume: bool) -> dict[str, Any]:
    if not resume or not path.exists():
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "hash_algorithm": HASH_ALGORITHM,
            "updated_at": utc_now(),
            "entries": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read checkpoint {path}: {error}") from error
    if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported checkpoint schema in {path}")
    if value.get("hash_algorithm") != HASH_ALGORITHM:
        raise ValueError(f"Checkpoint hash algorithm is not {HASH_ALGORITHM}")
    if not isinstance(value.get("entries"), dict):
        raise ValueError(f"Checkpoint entries are malformed in {path}")
    return value


def _checkpoint_hash(entry: Any, *, size: int, mtime_ns: int) -> str | None:
    if not isinstance(entry, dict):
        return None
    digest = entry.get("sha256")
    if (
        entry.get("size") == size
        and entry.get("mtime_ns") == mtime_ns
        and isinstance(digest, str)
        and SHA256_RE.fullmatch(digest)
    ):
        return digest
    return None


def _save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = utc_now()
    _atomic_write_json(path, checkpoint)


def _record_warning_codes(record: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    pdf = record.get("pdf")
    if isinstance(pdf, dict):
        if not pdf.get("magic_ok"):
            warnings.append("pdf_magic_missing")
        if pdf.get("parse_status") not in {"ok", "encrypted_empty_password"}:
            warnings.append(f"pdf_{pdf.get('parse_status', 'unknown')}")
        if pdf.get("encrypted"):
            warnings.append("pdf_encrypted")
        if pdf.get("page_count") is None:
            warnings.append("pdf_page_count_missing")
    if record.get("sha256") is None:
        warnings.append("sha256_missing")
    return warnings


def _record_stats(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(str(record.get("kind", "unknown")) for record in records)
    pdf_records = [record for record in records if record.get("kind") == "pdf"]
    parse_statuses = Counter(
        str((record.get("pdf") or {}).get("parse_status", "missing")) for record in pdf_records
    )
    return {
        "record_count": len(records),
        "by_kind": dict(sorted(kinds.items())),
        "pdf": {
            "count": len(pdf_records),
            "magic_ok": sum(bool((record.get("pdf") or {}).get("magic_ok")) for record in pdf_records),
            "encrypted": sum(bool((record.get("pdf") or {}).get("encrypted")) for record in pdf_records),
            "with_page_count": sum(
                (record.get("pdf") or {}).get("page_count") is not None for record in pdf_records
            ),
            "page_total": sum(
                int((record.get("pdf") or {}).get("page_count") or 0) for record in pdf_records
            ),
            "parse_status": dict(sorted(parse_statuses.items())),
        },
        "warning_records": sum(bool(record.get("warnings")) for record in records),
    }


def scan_roots(
    root_paths: Sequence[Path],
    *,
    output_prefix: Path | None = None,
    checkpoint_path: Path | None = None,
    core_root: Path | None = None,
    excluded_dir_names: Iterable[str] = (),
    shard_count: int = 1,
    shard_index: int = 0,
    max_files: int | None = None,
    resume: bool = True,
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
) -> dict[str, Any]:
    """Scan roots and optionally write JSON/CSV/Markdown reports.

    This function performs no writes when ``output_prefix`` and
    ``checkpoint_path`` are both ``None``.
    """

    if not root_paths:
        raise ValueError("At least one scan root is required")
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must satisfy 0 <= index < count")
    if max_files is not None and max_files < 1:
        raise ValueError("max_files must be at least 1")
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")

    roots = [_make_root_spec(path) for path in root_paths]
    if len({root.root_id for root in roots}) != len(roots):
        raise ValueError("Duplicate scan roots resolve to the same path")
    core = core_root.resolve(strict=True) if core_root is not None else None
    excludes = frozenset(DEFAULT_EXCLUDED_DIR_NAMES | {name.casefold() for name in excluded_dir_names})
    all_candidates, discovery_stats, errors = discover_candidates(roots, excludes)
    selected = [
        candidate
        for candidate in all_candidates
        if _candidate_shard(candidate, shard_count) == shard_index
    ]
    selected_before_limit = len(selected)
    if max_files is not None:
        selected = selected[:max_files]

    prefix = normalize_prefix(output_prefix) if output_prefix is not None else None
    if checkpoint_path is None and prefix is not None:
        checkpoint_path = Path(f"{prefix}.checkpoint.json")
    checkpoint = _load_checkpoint(checkpoint_path, resume) if checkpoint_path else {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "entries": {},
    }
    entries = checkpoint["entries"]
    records: list[dict[str, Any]] = []
    hashes_computed = 0
    hashes_reused = 0

    try:
        for position, candidate in enumerate(selected, start=1):
            path = Path(candidate.absolute_path)
            record: dict[str, Any] = {
                "record_id": candidate.record_id,
                "root_id": candidate.root_id,
                "root_path": candidate.root_path,
                "relative_path": candidate.relative_path,
                "absolute_path": candidate.absolute_path,
                "kind": candidate.kind,
                "root_in_core": _path_is_within(path, core),
                "size": None,
                "mtime_ns": None,
                "sha256": None,
                "hash_source": None,
                "pdf": None,
                "warnings": [],
            }
            try:
                file_stat = path.stat()
                record["size"] = file_stat.st_size
                record["mtime_ns"] = file_stat.st_mtime_ns
                cached_hash = _checkpoint_hash(
                    entries.get(candidate.record_id),
                    size=file_stat.st_size,
                    mtime_ns=file_stat.st_mtime_ns,
                )
                if cached_hash is not None:
                    record["sha256"] = cached_hash
                    record["hash_source"] = "checkpoint"
                    hashes_reused += 1
                else:
                    digest = _hash_file(path)
                    record["sha256"] = digest
                    record["hash_source"] = "computed"
                    hashes_computed += 1
                    entries[candidate.record_id] = {
                        "root_id": candidate.root_id,
                        "relative_path": candidate.relative_path,
                        "size": file_stat.st_size,
                        "mtime_ns": file_stat.st_mtime_ns,
                        "sha256": digest,
                    }
                if candidate.kind == "pdf":
                    record["pdf"] = inspect_pdf(path)
            except OSError as error:
                errors.append(
                    {
                        "stage": "inspect",
                        "root": candidate.root_path,
                        "path": candidate.relative_path,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
            record["warnings"] = _record_warning_codes(record)
            records.append(record)
            if checkpoint_path and position % checkpoint_every == 0:
                _save_checkpoint(checkpoint_path, checkpoint)
    finally:
        if checkpoint_path:
            _save_checkpoint(checkpoint_path, checkpoint)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "report_type": "file_census_shard" if shard_count > 1 else "file_census",
        "read_only_source_scan": True,
        "roots": [asdict(root) for root in roots],
        "core_root": str(core) if core is not None else None,
        "scan_options": {
            "excluded_directory_names": sorted(excludes),
            "follow_symlinks_or_junctions": False,
            "hash_algorithm": HASH_ALGORITHM,
            "hash_chunk_size": DEFAULT_HASH_CHUNK_SIZE,
            "resume": resume,
            "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
            "shard_count": shard_count,
            "shard_index": shard_index,
            "max_files": max_files,
        },
        "scope": {
            "complete": shard_count == 1 and max_files is None,
            "discovered_total": len(all_candidates),
            "selected_before_limit": selected_before_limit,
            "selected": len(selected),
        },
        "discovery": asdict(discovery_stats),
        "hashes": {"computed": hashes_computed, "reused_from_checkpoint": hashes_reused},
        "stats": _record_stats(records),
        "errors": errors,
        "records": records,
    }
    if prefix is not None:
        write_reports(prefix, report)
    return report


CSV_FIELDS = (
    "record_id",
    "root_id",
    "root_path",
    "relative_path",
    "absolute_path",
    "kind",
    "root_in_core",
    "size",
    "mtime_ns",
    "sha256",
    "hash_source",
    "pdf_magic_ok",
    "pdf_magic_offset",
    "pdf_parse_status",
    "pdf_encrypted",
    "pdf_page_count",
    "pdf_metadata_title",
    "pdf_metadata_author",
    "pdf_error",
    "warnings",
)


def _csv_rows(records: Sequence[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for record in records:
        pdf = record.get("pdf") if isinstance(record.get("pdf"), dict) else {}
        yield {
            "record_id": record.get("record_id"),
            "root_id": record.get("root_id"),
            "root_path": record.get("root_path"),
            "relative_path": record.get("relative_path"),
            "absolute_path": record.get("absolute_path"),
            "kind": record.get("kind"),
            "root_in_core": record.get("root_in_core"),
            "size": record.get("size"),
            "mtime_ns": record.get("mtime_ns"),
            "sha256": record.get("sha256"),
            "hash_source": record.get("hash_source"),
            "pdf_magic_ok": pdf.get("magic_ok"),
            "pdf_magic_offset": pdf.get("magic_offset"),
            "pdf_parse_status": pdf.get("parse_status"),
            "pdf_encrypted": pdf.get("encrypted"),
            "pdf_page_count": pdf.get("page_count"),
            "pdf_metadata_title": pdf.get("metadata_title"),
            "pdf_metadata_author": pdf.get("metadata_author"),
            "pdf_error": pdf.get("error"),
            "warnings": " | ".join(record.get("warnings") or []),
        }


def _render_markdown(report: dict[str, Any]) -> str:
    stats = report.get("stats") or {}
    pdf = stats.get("pdf") or {}
    scope = report.get("scope") or {}
    lines = [
        "# Core paper file census",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Schema: `{report.get('schema_version')}`",
        f"- Source scan is read-only: `{str(bool(report.get('read_only_source_scan'))).lower()}`",
        f"- Scope complete: `{str(bool(scope.get('complete'))).lower()}`",
        f"- Discovered candidates: `{scope.get('discovered_total', 0)}`",
        f"- Records in this report: `{stats.get('record_count', 0)}`",
        "",
        "## Roots",
        "",
    ]
    for root in report.get("roots") or []:
        lines.append(f"- `{root.get('path')}` (`{root.get('root_id')}`)")
    lines.extend(["", "## Representation counts", "", "| Kind | Count |", "|---|---:|"])
    for kind, count in sorted((stats.get("by_kind") or {}).items()):
        lines.append(f"| {kind} | {count} |")
    lines.extend(
        [
            "",
            "## PDF health",
            "",
            "| Check | Count |",
            "|---|---:|",
            f"| PDF records | {pdf.get('count', 0)} |",
            f"| `%PDF-` magic found | {pdf.get('magic_ok', 0)} |",
            f"| Page count available | {pdf.get('with_page_count', 0)} |",
            f"| Encrypted | {pdf.get('encrypted', 0)} |",
            f"| Total pages | {pdf.get('page_total', 0)} |",
            "",
            "### Parse status",
            "",
            "| Status | Count |",
            "|---|---:|",
        ]
    )
    for status_name, count in sorted((pdf.get("parse_status") or {}).items()):
        lines.append(f"| {status_name} | {count} |")
    discovery = report.get("discovery") or {}
    lines.extend(
        [
            "",
            "## Exclusions",
            "",
            f"- Excluded directories: `{discovery.get('directories_excluded', 0)}`",
            f"- Reparse/symlink directories skipped: `{discovery.get('reparse_directories_skipped', 0)}`",
            f"- Reparse/symlink files skipped: `{discovery.get('reparse_files_skipped', 0)}`",
            f"- Sensitive filenames skipped without reading: `{discovery.get('sensitive_files_skipped', 0)}`",
            "",
            "## Errors",
            "",
        ]
    )
    errors = report.get("errors") or []
    if not errors:
        lines.append("No discovery, stat, or hashing errors were recorded.")
    else:
        lines.extend(["| Stage | Path | Error |", "|---|---|---|"])
        for error in errors[:50]:
            path = str(error.get("path", "")).replace("|", "\\|")
            message = str(error.get("error", "")).replace("|", "\\|")
            lines.append(f"| {error.get('stage', '')} | `{path}` | {message} |")
        if len(errors) > 50:
            lines.append(f"\nOnly the first 50 of {len(errors)} errors are shown; see JSON for all entries.")
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "This is a file-level evidence census. Canonical paper identity, disposition, domain routing,",
            "and PDF-to-HTML/code closure decisions must be added by the subsequent paper-level review.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(output_prefix: Path, report: dict[str, Any]) -> tuple[Path, Path, Path]:
    prefix = normalize_prefix(output_prefix)
    json_path = Path(f"{prefix}.json")
    csv_path = Path(f"{prefix}.csv")
    markdown_path = Path(f"{prefix}.md")
    _atomic_write_json(json_path, report)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_csv = csv_path.with_name(f".{csv_path.name}.tmp-{os.getpid()}")
    try:
        with temporary_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(_csv_rows(report.get("records") or []))
        os.replace(temporary_csv, csv_path)
    finally:
        if temporary_csv.exists():
            temporary_csv.unlink()
    _atomic_write_text(markdown_path, _render_markdown(report))
    return json_path, csv_path, markdown_path


def merge_reports(input_paths: Sequence[Path], *, output_prefix: Path | None = None) -> dict[str, Any]:
    """Merge non-overlapping shard JSON reports and detect conflicting duplicates."""

    if not input_paths:
        raise ValueError("At least one shard report is required")
    reports: list[dict[str, Any]] = []
    for input_path in input_paths:
        value = json.loads(input_path.read_text(encoding="utf-8"))
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported census schema in {input_path}")
        reports.append(value)

    records_by_id: dict[str, dict[str, Any]] = {}
    for report in reports:
        for record in report.get("records") or []:
            record_id = str(record.get("record_id", ""))
            if not record_id:
                raise ValueError("A shard contains a record without record_id")
            previous = records_by_id.get(record_id)
            if previous is not None and previous != record:
                raise ValueError(f"Conflicting duplicate record {record_id}")
            records_by_id[record_id] = record
    records = sorted(
        records_by_id.values(),
        key=lambda record: (
            str(record.get("root_id", "")),
            str(record.get("relative_path", "")).casefold(),
            str(record.get("relative_path", "")),
        ),
    )

    roots_by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    discovery_totals: Counter[str] = Counter()
    for report in reports:
        for root in report.get("roots") or []:
            root_id = str(root.get("root_id", ""))
            prior = roots_by_id.get(root_id)
            if prior is not None and prior != root:
                raise ValueError(f"Conflicting root definition {root_id}")
            roots_by_id[root_id] = root
        errors.extend(report.get("errors") or [])
        discovery_totals.update(
            {key: int(value) for key, value in (report.get("discovery") or {}).items()}
        )

    shard_pairs = {
        (
            int((report.get("scan_options") or {}).get("shard_index", 0)),
            int((report.get("scan_options") or {}).get("shard_count", 1)),
        )
        for report in reports
    }
    shard_counts = {count for _, count in shard_pairs}
    complete_shard_set = False
    if len(shard_counts) == 1:
        shard_count = next(iter(shard_counts))
        indices = {index for index, _ in shard_pairs}
        no_limits = all((report.get("scan_options") or {}).get("max_files") is None for report in reports)
        complete_shard_set = indices == set(range(shard_count)) and no_limits

    merged: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "report_type": "file_census_merged",
        "read_only_source_scan": True,
        "roots": sorted(roots_by_id.values(), key=lambda root: str(root.get("root_id", ""))),
        "core_root": reports[0].get("core_root"),
        "scan_options": {
            "merged_inputs": [str(path.resolve()) for path in input_paths],
            "input_shards": sorted([list(pair) for pair in shard_pairs]),
        },
        "scope": {
            "complete": complete_shard_set,
            "discovered_total": len(records),
            "selected_before_limit": len(records),
            "selected": len(records),
        },
        # Each shard walks the same roots, so summed exclusion counts are labelled
        # explicitly and must not be interpreted as unique filesystem objects.
        "discovery": {
            **dict(discovery_totals),
            "note": "counts are summed across shard scans and may repeat excluded paths",
        },
        "hashes": {
            "computed": sum(int((report.get("hashes") or {}).get("computed", 0)) for report in reports),
            "reused_from_checkpoint": sum(
                int((report.get("hashes") or {}).get("reused_from_checkpoint", 0))
                for report in reports
            ),
        },
        "stats": _record_stats(records),
        "errors": errors,
        "records": records,
    }
    if output_prefix is not None:
        write_reports(output_prefix, merged)
    return merged


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a resumable read-only census of paper-library representations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="scan one or more roots")
    scan_parser.add_argument("roots", nargs="+", type=Path)
    scan_parser.add_argument("--output-prefix", type=Path, required=True)
    scan_parser.add_argument("--checkpoint", type=Path)
    scan_parser.add_argument("--core-root", type=Path)
    scan_parser.add_argument("--exclude-dir", action="append", default=[])
    scan_parser.add_argument("--shard-count", type=int, default=1)
    scan_parser.add_argument("--shard-index", type=int, default=0)
    scan_parser.add_argument("--max-files", type=int)
    scan_parser.add_argument("--no-resume", action="store_true")
    scan_parser.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY)
    scan_parser.add_argument(
        "--strict-pdf",
        action="store_true",
        help="return a non-zero exit code if a PDF lacks magic, parsing, or page count",
    )

    merge_parser = subparsers.add_parser("merge", help="merge JSON reports from all shards")
    merge_parser.add_argument("inputs", nargs="+", type=Path)
    merge_parser.add_argument("--output-prefix", type=Path, required=True)
    return parser


def _print_summary(report: dict[str, Any], prefix: Path) -> None:
    stats = report.get("stats") or {}
    hashes = report.get("hashes") or {}
    print(
        f"records={stats.get('record_count', 0)} "
        f"pdf={((stats.get('pdf') or {}).get('count', 0))} "
        f"hash_computed={hashes.get('computed', 0)} "
        f"hash_reused={hashes.get('reused_from_checkpoint', 0)} "
        f"errors={len(report.get('errors') or [])}"
    )
    print(f"reports={normalize_prefix(prefix)}.[json|csv|md]")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            report = scan_roots(
                args.roots,
                output_prefix=args.output_prefix,
                checkpoint_path=args.checkpoint,
                core_root=args.core_root,
                excluded_dir_names=args.exclude_dir,
                shard_count=args.shard_count,
                shard_index=args.shard_index,
                max_files=args.max_files,
                resume=not args.no_resume,
                checkpoint_every=args.checkpoint_every,
            )
            _print_summary(report, args.output_prefix)
            if report.get("errors"):
                return 2
            if args.strict_pdf:
                pdf_stats = (report.get("stats") or {}).get("pdf") or {}
                if (
                    pdf_stats.get("magic_ok", 0) != pdf_stats.get("count", 0)
                    or pdf_stats.get("with_page_count", 0) != pdf_stats.get("count", 0)
                    or set((pdf_stats.get("parse_status") or {})) - {"ok", "encrypted_empty_password"}
                ):
                    return 3
            return 0
        report = merge_reports(args.inputs, output_prefix=args.output_prefix)
        _print_summary(report, args.output_prefix)
        return 2 if report.get("errors") else 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"paper-census: {error}\n")
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
