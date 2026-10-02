"""Read-only verification of registered source representations.

The verifier deliberately resolves every local representation through the asset
gateway's :class:`~apps.asset_gateway.registry.SourceRegistry`.  Reports contain
only opaque representation IDs, source aliases, relative references, and reason
codes; resolved source roots and file contents are never serialized.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.enums import MaterializationKind, RepresentationAvailability
from packages.domain.models import RepositoryProfile, Representation, Source
from packages.domain.serving_scope import active_representation_clause
from packages.ingestion import safe_tree_policy

ServingScope = Literal["serving_active", "all"]

_LOCAL_KINDS = frozenset(
    {
        MaterializationKind.FILE.value,
        MaterializationKind.DIRECTORY.value,
        MaterializationKind.BUNDLE.value,
    }
)
_URL_KINDS = frozenset({MaterializationKind.URL.value})


@dataclass(frozen=True, slots=True)
class SourceIntegrityMismatch:
    """A compact, path-safe mismatch entry for a representation."""

    representation_id: UUID
    source_alias: str | None
    path_ref: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        reason = ";".join(self.reasons)
        return {
            "representation_id": str(self.representation_id),
            "source_alias": self.source_alias,
            "path_ref": self.path_ref,
            "reason": reason,
            "reasons": list(self.reasons),
        }


@dataclass(slots=True)
class SourceIntegrityReport:
    """Result of one read-only source integrity pass."""

    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    scope: ServingScope = "serving_active"
    counts: dict[str, int] = field(default_factory=dict)
    population: dict[str, int] = field(default_factory=dict)
    mismatches: list[SourceIntegrityMismatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        mismatch_count = len(self.mismatches)
        counts = {
            "representations_seen": 0,
            "files_verified": 0,
            "directories_verified": 0,
            "missing_confirmed": 0,
            "external_skipped": 0,
            "mismatches": mismatch_count,
            **self.counts,
        }
        counts["mismatches"] = mismatch_count
        return {
            "schema": "research-asset-hub.source-integrity/v1",
            "scope": self.scope,
            "status": "passed" if mismatch_count == 0 else "failed",
            "generated_at": self.generated_at.isoformat(),
            "read_only": True,
            "population": dict(self.population),
            "counts": counts,
            "mismatches": [item.to_dict() for item in self.mismatches],
        }


def _safe_path_ref(value: str | None) -> str | None:
    """Normalize a relative path without ever returning an absolute path."""

    if value is None:
        return None
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return "<unsafe>"
    return candidate.as_posix()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory_tree_digest(path: Path) -> str:
    """Match formal importer's deterministic path/kind/stat tree digest exactly."""

    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
        try:
            stat = child.stat()
        except OSError:
            continue
        relative = child.relative_to(path).as_posix()
        kind = "d" if child.is_dir() else "f"
        digest.update(f"{kind}\0{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _git_worktree_matches_recorded_revision(
    path: Path,
    attributes: dict[str, Any],
) -> bool:
    """Accept volatile ``.git`` metadata only for a clean recorded revision."""

    expected = str(attributes.get("commit") or "").strip()
    if not expected or not (path / ".git").is_dir():
        return False
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=30,
            creationflags=creation_flags,
        )
        status = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(path),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=30,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    actual = head.stdout.strip()
    revision_matches = (
        bool(actual)
        and head.returncode == 0
        and (
            actual.casefold() == expected.casefold()
            or actual.casefold().startswith(expected.casefold())
            or expected.casefold().startswith(actual.casefold())
        )
    )
    if not revision_matches or status.returncode != 0:
        return False
    entries = [value for value in status.stdout.split("\0") if value]
    if not entries:
        return True
    # Some historical source clones committed full Git-LFS payloads while
    # declaring LFS filters afterwards. Git reports those byte-identical files
    # as modified. Accept only this narrow case: the index is clean, every
    # reported worktree path exists, and its raw bytes equal the HEAD blob.
    paths: list[str] = []
    for entry in entries:
        if len(entry) < 4:
            return False
        relative = entry[3:]
        reference = PurePosixPath(relative.replace("\\", "/"))
        if reference.is_absolute() or ".." in reference.parts:
            return False
        if entry[:2] == "??" and reference.parts[0] == ".gitnexus":
            metadata_root = path / ".gitnexus"
            try:
                metadata_stat = os.lstat(metadata_root)
            except OSError:
                return False
            if (
                safe_tree_policy.is_reparse_stat(metadata_stat)
                or not metadata_root.is_dir()
            ):
                return False
            continue
        if entry[:2] != " M":
            return False
        paths.append(reference.as_posix())
    return all(_worktree_file_matches_head_blob(path, relative) for relative in paths)


def _worktree_file_matches_head_blob(path: Path, relative: str) -> bool:
    candidate = path.joinpath(*PurePosixPath(relative).parts)
    if not candidate.is_file() or candidate.is_symlink():
        return False
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "cat-file", "blob", f"HEAD:{relative}"],
            check=False,
            capture_output=True,
            timeout=120,
            creationflags=creation_flags,
        )
        if completed.returncode != 0:
            return False
        head_digest = hashlib.sha256(completed.stdout).hexdigest()
        return (
            len(completed.stdout) == candidate.stat().st_size
            and head_digest == _file_sha256(candidate)
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _only_git_metadata_changed_since(path: Path, baseline: datetime) -> bool:
    """Reject fallback when any worktree entry is newer than the import."""

    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=UTC)
    threshold = int(baseline.timestamp() * 1_000_000_000)
    try:
        for child in path.rglob("*"):
            relative = child.relative_to(path)
            if relative.parts and relative.parts[0] in {".git", ".gitnexus"}:
                continue
            if child.stat().st_mtime_ns > threshold:
                return False
    except (OSError, ValueError):
        return False
    return True


def _source_attributes(representation: Representation) -> dict[str, Any]:
    attributes = representation.attributes
    return dict(attributes) if isinstance(attributes, dict) else {}


def _mismatch(
    representation: Representation,
    source_alias: str | None,
    reasons: list[str],
) -> SourceIntegrityMismatch:
    return SourceIntegrityMismatch(
        representation_id=representation.id,
        source_alias=source_alias,
        path_ref=_safe_path_ref(representation.path_ref),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def verify_source_integrity(
    session: Session,
    registry: SourceRegistry,
    *,
    scope: ServingScope = "serving_active",
) -> SourceIntegrityReport:
    """Verify every active, locally materialized representation in ``session``.

    URL representations are intentionally skipped.  Missing representations are
    considered healthy only while their registered local path remains absent.
    File and directory fingerprints are recomputed once per resolved location per
    pass, so repeated mounts do not cause repeated reads.
    """

    normalized_scope = _normalize_scope(scope)
    report = SourceIntegrityReport(scope=normalized_scope)
    resolution_cache: dict[tuple[str, str, str], Path | UnsafeAssetPath] = {}
    file_digest_cache: dict[tuple[str, int, int], str] = {}
    directory_digest_cache: dict[str, str] = {}

    base_statement = (
        select(Representation, Source.alias)
        .outerjoin(Source, Representation.source_id == Source.id)
        .where(Representation.deleted_at.is_(None))
        .order_by(Representation.id)
    )
    registered_non_deleted = session.execute(base_statement).all()
    serving_active = session.execute(
        base_statement.where(active_representation_clause())
    ).all()
    representations = (
        registered_non_deleted if normalized_scope == "all" else serving_active
    )
    report.population.update(
        {
            "registered_non_deleted": len(registered_non_deleted),
            "serving_active": len(serving_active),
            "retired_historical": len(registered_non_deleted) - len(serving_active),
        }
    )
    report.counts["representations_seen"] = len(representations)
    repository_revisions = {
        asset_id: str(revision)
        for asset_id, revision in session.execute(
            select(RepositoryProfile.asset_id, RepositoryProfile.pinned_revision).where(
                RepositoryProfile.pinned_revision.is_not(None)
            )
        )
        if revision
    }

    for representation, source_alias_value in representations:
        source_alias = str(source_alias_value) if source_alias_value is not None else None
        materialization = str(representation.materialization_kind)
        if materialization in _URL_KINDS or (
            materialization not in _LOCAL_KINDS and representation.path_ref is None
        ):
            report.counts["external_skipped"] = report.counts.get("external_skipped", 0) + 1
            continue

        if source_alias is None:
            report.mismatches.append(_mismatch(representation, None, ["source_missing"]))
            continue
        if representation.path_ref is None:
            report.mismatches.append(
                _mismatch(representation, source_alias, ["path_ref_missing"])
            )
            continue

        cache_key = (source_alias, representation.root_key, representation.path_ref)
        resolved = resolution_cache.get(cache_key)
        if resolved is None:
            try:
                resolved = registry.resolve(
                    source_alias,
                    representation.root_key,
                    representation.path_ref,
                )
            except (OSError, UnsafeAssetPath, ValueError):
                resolved = UnsafeAssetPath("Registered path could not be resolved.")
            resolution_cache[cache_key] = resolved
        if isinstance(resolved, UnsafeAssetPath):
            report.mismatches.append(
                _mismatch(representation, source_alias, ["unsafe_registered_path"])
            )
            continue

        path = resolved
        availability = str(representation.availability)
        try:
            exists = path.exists()
        except (OSError, ValueError):
            exists = False

        if availability == RepresentationAvailability.MISSING.value:
            if exists:
                report.mismatches.append(
                    _mismatch(representation, source_alias, ["missing_path_exists"])
                )
            else:
                report.counts["missing_confirmed"] = (
                    report.counts.get("missing_confirmed", 0) + 1
                )
            continue

        if availability != RepresentationAvailability.AVAILABLE.value:
            report.mismatches.append(
                _mismatch(
                    representation,
                    source_alias,
                    [f"availability_unverifiable:{availability}"],
                )
            )
            continue

        try:
            stat = path.stat()
        except (OSError, ValueError):
            report.mismatches.append(
                _mismatch(representation, source_alias, ["path_missing"])
            )
            continue

        attributes = _source_attributes(representation)
        if "commit" not in attributes:
            pinned_revision = repository_revisions.get(representation.asset_id)
            if pinned_revision:
                attributes["commit"] = pinned_revision
        reasons: list[str] = []
        if materialization == MaterializationKind.FILE.value:
            if not path.is_file():
                reasons.append("expected_file")
            else:
                expected_size = attributes.get("_source_size")
                expected_mtime = attributes.get("_source_mtime_ns")
                if expected_size != stat.st_size:
                    reasons.append("size_mismatch")
                if expected_mtime != stat.st_mtime_ns:
                    reasons.append("mtime_ns_mismatch")
                cache_key_file = (str(path), stat.st_size, stat.st_mtime_ns)
                actual_sha256 = file_digest_cache.get(cache_key_file)
                if actual_sha256 is None:
                    try:
                        actual_sha256 = _file_sha256(path)
                    except (OSError, ValueError):
                        reasons.append("sha256_unreadable")
                    else:
                        file_digest_cache[cache_key_file] = actual_sha256
                if actual_sha256 is not None:
                    if representation.sha256 is None:
                        reasons.append("sha256_missing")
                    elif representation.sha256.casefold() != actual_sha256.casefold():
                        reasons.append("sha256_mismatch")
            if reasons:
                report.mismatches.append(_mismatch(representation, source_alias, reasons))
            else:
                report.counts["files_verified"] = report.counts.get("files_verified", 0) + 1
            continue

        if materialization not in {
            MaterializationKind.DIRECTORY.value,
            MaterializationKind.BUNDLE.value,
        }:
            report.mismatches.append(
                _mismatch(representation, source_alias, ["unsupported_materialization"])
            )
            continue

        if not path.is_dir():
            reasons.append("expected_directory")
        else:
            expected_mtime = attributes.get("_source_mtime_ns")
            if expected_mtime != stat.st_mtime_ns:
                reasons.append("mtime_ns_mismatch")
            digest_key = str(path)
            actual_tree_digest = directory_digest_cache.get(digest_key)
            if actual_tree_digest is None:
                try:
                    actual_tree_digest = _directory_tree_digest(path)
                except (OSError, ValueError):
                    reasons.append("tree_digest_unreadable")
                else:
                    directory_digest_cache[digest_key] = actual_tree_digest
            if actual_tree_digest is not None:
                if representation.tree_digest is None:
                    reasons.append("tree_digest_missing")
                elif representation.tree_digest.casefold() != actual_tree_digest.casefold():
                    if (
                        _only_git_metadata_changed_since(
                            path,
                            representation.created_at,
                        )
                        and _git_worktree_matches_recorded_revision(path, attributes)
                    ):
                        report.counts["git_metadata_drift_ignored"] = (
                            report.counts.get("git_metadata_drift_ignored", 0) + 1
                        )
                    else:
                        reasons.append("tree_digest_mismatch")
        if reasons:
            report.mismatches.append(_mismatch(representation, source_alias, reasons))
        else:
            report.counts["directories_verified"] = (
                report.counts.get("directories_verified", 0) + 1
            )

    return report


def _normalize_scope(value: str) -> ServingScope:
    if value == "serving_active":
        return "serving_active"
    if value == "all":
        return "all"
    raise ValueError("scope must be 'serving_active' or 'all'")


__all__ = [
    "SourceIntegrityMismatch",
    "SourceIntegrityReport",
    "verify_source_integrity",
]
