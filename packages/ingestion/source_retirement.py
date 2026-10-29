"""Signed, append-only source retirement with complete successor coverage.

The service never rewrites frozen source configuration, manifests, source
records, representations, or mounts.  Applying a validated plan only appends
retirement evidence and disables the retired :class:`Source`.  A rollback is a
second signed append-only event that re-enables that source.

Apply and rollback own their SERIALIZABLE transaction.  Call them with a fresh
``Session`` (no transaction already in progress); preview calls are read-only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Exists, exists, or_, select, text
from sqlalchemy.orm import Session

from packages.domain.enums import BackupStatus
from packages.domain.identifiers import new_uuid7
from packages.domain.models import (
    BackupRun,
    Representation,
    RepresentationMount,
    Source,
    SourceRecord,
    SourceRetirement,
    SourceRetirementMountMap,
    SourceRetirementRecordMap,
    SourceRetirementRepresentationMap,
    SourceRetirementRollback,
    SourceRetirementSuccessor,
)
from packages.ingestion import safe_tree_policy

_FROZEN_MANIFEST_SHA256 = "_frozen_manifest_sha256"
_FROZEN_SOURCE_CONFIG_SHA256 = "_frozen_source_config_sha256"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1
_SAFE_TREE_POLICY_VERSION = safe_tree_policy.SAFE_TREE_POLICY_VERSION


class SourceRetirementError(RuntimeError):
    """Base class for retirement safety failures."""


class SourceRetirementValidationError(SourceRetirementError):
    """Raised when coverage, equivalence, backup, or frozen state is invalid."""


class SourceRetirementDriftError(SourceRetirementError):
    """Raised when database state no longer matches the signed preview."""


class SourceRetirementSignatureError(SourceRetirementError):
    """Raised when a retirement or rollback signature is rejected."""


class SourceRetirementTransactionError(SourceRetirementError):
    """Raised when a caller supplies a session with an existing transaction."""


class SignatureVerifier(Protocol):
    """Injectable verifier; production key lookup stays outside this module."""

    def __call__(
        self,
        *,
        payload: bytes,
        signature: bytes,
        key_id: str,
        algorithm: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class RetirementIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class SourceRetirementPreview:
    payload: dict[str, Any]
    payload_digest: str
    issues: tuple[RetirementIssue, ...]
    warnings: tuple[RetirementIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "invalid",
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "issues": [asdict(item) for item in self.issues],
            "warnings": [asdict(item) for item in self.warnings],
            "counts": {
                "records": len(self.payload.get("record_mappings", [])),
                "representations": len(
                    self.payload.get("representation_mappings", [])
                ),
                "mounts": len(self.payload.get("mount_mappings", [])),
                "successor_sources": len(self.payload.get("successors", [])),
            },
        }


@dataclass(frozen=True, slots=True)
class SignedSourceRetirementPreview:
    """One canonical preview plus the detached signature applied to its bytes."""

    preview: SourceRetirementPreview
    signature: bytes
    signer_key_id: str
    signature_algorithm: str = "ed25519"


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the sole byte encoding accepted for digests and signatures."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def active_retirement_exists(source_id: Any) -> Exists:
    """SQL expression for a UUID value or a correlated ``source_id`` column."""

    rolled_back = exists(
        select(SourceRetirementRollback.id).where(
            SourceRetirementRollback.retirement_id == SourceRetirement.id
        )
    )
    return exists(
        select(SourceRetirement.id).where(
            SourceRetirement.source_id == source_id,
            ~rolled_back,
        )
    )


def is_source_retired(session: Session, source_id: UUID) -> bool:
    return bool(session.scalar(select(active_retirement_exists(source_id))))


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _uuid_mapping(values: Mapping[Any, Any]) -> dict[UUID, UUID]:
    return {_uuid(key): _uuid(value) for key, value in values.items()}


def _datetime(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _record_state(record: SourceRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "source_id": str(record.source_id),
        "source_record_id": record.source_record_id,
        "asset_id": None if record.asset_id is None else str(record.asset_id),
        "outcome": record.outcome,
        "raw_data": record.raw_data,
        "source_fields": record.source_fields,
        "normalized_digest": record.normalized_digest,
        "warnings": record.warnings,
        "row_version": record.row_version,
    }


def _representation_state(representation: Representation) -> dict[str, Any]:
    return {
        "id": str(representation.id),
        "source_id": (
            None if representation.source_id is None else str(representation.source_id)
        ),
        "source_record_id": (
            None
            if representation.source_record_id is None
            else str(representation.source_record_id)
        ),
        "asset_id": str(representation.asset_id),
        "representation_type": representation.representation_type,
        "role": representation.role,
        "materialization_kind": representation.materialization_kind,
        "availability": representation.availability,
        "root_key": representation.root_key,
        "path_ref": representation.path_ref,
        "external_url": representation.external_url,
        "entrypoint": representation.entrypoint,
        "revision": representation.revision,
        "tree_digest": representation.tree_digest,
        "sha256": representation.sha256,
        "mime_type": representation.mime_type,
        "size_bytes": representation.size_bytes,
        "network_required": representation.network_required,
        "attributes": representation.attributes,
        "verified_at": _datetime(representation.verified_at),
        "deleted_at": _datetime(representation.deleted_at),
        "row_version": representation.row_version,
    }


def _mount_state(mount: RepresentationMount) -> dict[str, Any]:
    return {
        "id": str(mount.id),
        "representation_id": str(mount.representation_id),
        "mount_path": mount.mount_path,
        "source_id": str(mount.source_id),
        "root_key": mount.root_key,
        "path_ref": mount.path_ref,
        "read_only": mount.read_only,
        "priority": mount.priority,
        "deleted_at": _datetime(mount.deleted_at),
        "row_version": mount.row_version,
    }


def _source_state(source: Source) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "alias": source.alias,
        "adapter": source.adapter,
        "read_only": source.read_only,
        "enabled": source.enabled,
        "frozen_manifest_sha256": source.config.get(_FROZEN_MANIFEST_SHA256),
        "frozen_source_config_sha256": source.config.get(
            _FROZEN_SOURCE_CONFIG_SHA256
        ),
        "row_version": source.row_version,
    }


def _backup_state(backup: BackupRun) -> dict[str, Any]:
    return {
        "id": str(backup.id),
        "status": backup.status,
        "backup_kind": backup.backup_kind,
        "destination_label": backup.destination_label,
        "manifest_path": backup.manifest_path,
        "database_digest": backup.database_digest,
        "export_digest": backup.export_digest,
        "local_copy_only": backup.local_copy_only,
        "verified_at": _datetime(backup.verified_at),
        "restored_at": _datetime(backup.restored_at),
        "row_version": backup.row_version,
    }


def _state_digest(payload: Mapping[str, Any]) -> str:
    return canonical_payload_digest(payload)


def _valid_frozen_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower()
    return normalized if _SHA256_RE.fullmatch(normalized) else None


@dataclass(frozen=True, slots=True)
class _SafeTreeExpectation:
    successor_representation_id: UUID
    tree_sha256: str
    file_count: int
    aggregate_size_bytes: int
    policy_version: str
    policy_sha256: str


class _SafeTreeVerificationError(RuntimeError):
    """A local directory cannot be proved equivalent under the safe-copy policy."""


def _safe_tree_policy_sha256() -> str:
    """Fingerprint the exact external-copy allowlist used by retirement scans."""

    return safe_tree_policy.policy_sha256()


def _strict_nonnegative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceRetirementValidationError(
            f"safe-tree {label} must be a non-negative integer"
        )
    return int(value)


def _safe_tree_expectation_mapping(
    values: Mapping[Any, Mapping[str, Any]] | None,
) -> dict[UUID, _SafeTreeExpectation]:
    if values is None:
        return {}
    result: dict[UUID, _SafeTreeExpectation] = {}
    current_policy_sha256 = _safe_tree_policy_sha256()
    allowed_fields = {
        "successor_representation_id",
        "tree_sha256",
        "file_count",
        "aggregate_size_bytes",
        "policy_version",
        "policy_sha256",
    }
    for raw_id, raw in values.items():
        retired_id = _uuid(raw_id)
        if retired_id in result:
            raise SourceRetirementValidationError(
                f"duplicate safe-tree expectation for representation {retired_id}"
            )
        if not isinstance(raw, Mapping):
            raise SourceRetirementValidationError(
                "safe-tree expectation entries must be objects"
            )
        unknown = set(raw) - allowed_fields
        if unknown:
            raise SourceRetirementValidationError(
                "safe-tree expectation has unsupported fields: "
                + ", ".join(sorted(map(str, unknown)))
            )
        digest = raw.get("tree_sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise SourceRetirementValidationError(
                "safe-tree tree_sha256 must be a lowercase SHA-256"
            )
        policy_version = raw.get("policy_version", _SAFE_TREE_POLICY_VERSION)
        policy_sha256 = raw.get("policy_sha256", current_policy_sha256)
        if policy_version != _SAFE_TREE_POLICY_VERSION:
            raise SourceRetirementValidationError(
                f"unsupported safe-tree policy version {policy_version!r}"
            )
        if policy_sha256 != current_policy_sha256:
            raise SourceRetirementValidationError(
                "safe-tree policy fingerprint does not match the running policy"
            )
        try:
            successor_id = _uuid(raw["successor_representation_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise SourceRetirementValidationError(
                "safe-tree expectation requires a successor representation UUID"
            ) from error
        result[retired_id] = _SafeTreeExpectation(
            successor_representation_id=successor_id,
            tree_sha256=digest,
            file_count=_strict_nonnegative_int(
                raw.get("file_count"), label="file_count"
            ),
            aggregate_size_bytes=_strict_nonnegative_int(
                raw.get("aggregate_size_bytes"), label="aggregate_size_bytes"
            ),
            policy_version=policy_version,
            policy_sha256=policy_sha256,
        )
    return result


def _absolute_without_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_real_path_ancestry(path: Path) -> None:
    """Reject links/reparse points without first resolving through them."""

    absolute = _absolute_without_resolution(path)
    anchor = Path(absolute.anchor)
    try:
        relative = absolute.relative_to(anchor)
    except ValueError as error:
        raise _SafeTreeVerificationError("path_has_no_stable_anchor") from error
    current = anchor
    for part in relative.parts:
        current /= part
        try:
            value = os.lstat(current)
        except OSError as error:
            raise _SafeTreeVerificationError(
                f"path_lstat_failed:{error.__class__.__name__}"
            ) from error
        if safe_tree_policy.is_reparse_stat(value):
            raise _SafeTreeVerificationError("reparse_point")


def _representation_directory(
    session: Session,
    representation: Representation,
) -> Path:
    if (
        representation.materialization_kind != "directory"
        or representation.source_id is None
        or not representation.path_ref
    ):
        raise _SafeTreeVerificationError("not_an_explicit_local_directory")
    source = session.get(Source, representation.source_id)
    if source is None:
        raise _SafeTreeVerificationError("source_missing")
    source_root = Path(source.root_path)
    if not source_root.is_absolute():
        raise _SafeTreeVerificationError("source_root_not_absolute")
    if representation.root_key == "source":
        root = source_root
    else:
        asset_roots = source.config.get("asset_roots")
        if not isinstance(asset_roots, Mapping):
            raise _SafeTreeVerificationError("asset_roots_missing")
        raw_root = asset_roots.get(representation.root_key)
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise _SafeTreeVerificationError("asset_root_missing")
        configured = Path(raw_root)
        if configured.is_absolute():
            root = configured
        else:
            configured_ref = PurePosixPath(raw_root.replace("\\", "/"))
            if configured_ref.is_absolute() or any(
                part in {"", ".", ".."} for part in configured_ref.parts
            ):
                raise _SafeTreeVerificationError("asset_root_unsafe")
            root = source_root.joinpath(*configured_ref.parts)

    path_ref = PurePosixPath(representation.path_ref.replace("\\", "/"))
    if path_ref.is_absolute() or any(
        part in {"", ".", ".."} for part in path_ref.parts
    ):
        raise _SafeTreeVerificationError("path_ref_unsafe")
    root = _absolute_without_resolution(root)
    candidate = _absolute_without_resolution(root.joinpath(*path_ref.parts))
    if not safe_tree_policy.is_within(root, candidate):
        raise _SafeTreeVerificationError("path_escapes_asset_root")
    _assert_real_path_ancestry(candidate)
    try:
        value = os.lstat(candidate)
    except OSError as error:
        raise _SafeTreeVerificationError(
            f"directory_lstat_failed:{error.__class__.__name__}"
        ) from error
    if safe_tree_policy.is_reparse_stat(value) or not stat.S_ISDIR(value.st_mode):
        raise _SafeTreeVerificationError("not_a_real_directory")
    if safe_tree_policy.safety_reason(candidate, is_directory=True) is not None:
        raise _SafeTreeVerificationError("directory_rejected_by_safe_copy_policy")
    return candidate


def _safe_directory_summary(
    session: Session,
    representation: Representation,
) -> dict[str, Any]:
    """Hash only files the already-tested external-copy policy may copy."""

    directory = _representation_directory(session, representation)
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    files = safe_tree_policy.scan_directory(directory, directory, skipped, errors)
    if errors:
        raise _SafeTreeVerificationError("safe_scan_error")
    unsafe_skip_reasons = sorted(
        {
            str(item.get("reason", ""))
            for item in skipped
            if item.get("reason") not in safe_tree_policy.ALLOWED_EXCLUSION_REASONS
        }
    )
    if unsafe_skip_reasons:
        raise _SafeTreeVerificationError(
            "unsafe_skipped_entry:" + ",".join(unsafe_skip_reasons)
        )
    return {
        "tree_sha256": safe_tree_policy.content_tree_digest(files),
        "file_count": len(files),
        "aggregate_size_bytes": sum(int(item["size_bytes"]) for item in files),
    }


def _frozen_digests(
    source: Source,
    issues: list[RetirementIssue],
    *,
    role: str,
) -> tuple[str, str]:
    manifest = _valid_frozen_digest(source.config.get(_FROZEN_MANIFEST_SHA256))
    config = _valid_frozen_digest(source.config.get(_FROZEN_SOURCE_CONFIG_SHA256))
    if manifest is None:
        issues.append(
            RetirementIssue(
                "missing_frozen_manifest_digest",
                f"{role} source {source.alias!r} lacks a valid frozen manifest SHA-256.",
            )
        )
    if config is None:
        issues.append(
            RetirementIssue(
                "missing_frozen_config_digest",
                f"{role} source {source.alias!r} lacks a valid frozen config SHA-256.",
            )
        )
    return manifest or "", config or ""


def _content_evidence(
    session: Session,
    retired: Representation,
    successor: Representation,
    issues: list[RetirementIssue],
    *,
    safe_tree_expectation: _SafeTreeExpectation | None = None,
) -> dict[str, Any]:
    if retired.sha256 is not None or successor.sha256 is not None:
        left = _valid_frozen_digest(retired.sha256)
        right = _valid_frozen_digest(successor.sha256)
        if left is None or right is None or left != right:
            issues.append(
                RetirementIssue(
                    "representation_content_mismatch",
                    f"representation {retired.id} and {successor.id} "
                    "have different SHA-256 values.",
                )
            )
            return {"kind": "sha256", "value": left or ""}
        return {"kind": "sha256", "value": left}
    if retired.tree_digest is not None or successor.tree_digest is not None:
        left = _valid_frozen_digest(retired.tree_digest)
        right = _valid_frozen_digest(successor.tree_digest)
        if left is None or right is None or left != right:
            if safe_tree_expectation is not None:
                expected = safe_tree_expectation
                failure_evidence: dict[str, Any] = {
                    "kind": "safe_tree_sha256_invalid",
                    "value": expected.tree_sha256,
                    "file_count": expected.file_count,
                    "aggregate_size_bytes": expected.aggregate_size_bytes,
                    "policy_version": expected.policy_version,
                    "policy_sha256": expected.policy_sha256,
                }
                if expected.successor_representation_id != successor.id:
                    issues.append(
                        RetirementIssue(
                            "representation_safe_tree_invalid",
                            f"representation {retired.id} safe-tree expectation names "
                            "a different successor.",
                        )
                    )
                    return failure_evidence
                try:
                    retired_summary = _safe_directory_summary(session, retired)
                    successor_summary = _safe_directory_summary(session, successor)
                except _SafeTreeVerificationError as error:
                    issues.append(
                        RetirementIssue(
                            "representation_safe_tree_invalid",
                            f"representation {retired.id} safe-tree verification "
                            f"failed closed ({error}).",
                        )
                    )
                    return failure_evidence
                declared_summary = {
                    "tree_sha256": expected.tree_sha256,
                    "file_count": expected.file_count,
                    "aggregate_size_bytes": expected.aggregate_size_bytes,
                }
                if (
                    retired_summary != declared_summary
                    or successor_summary != declared_summary
                ):
                    issues.append(
                        RetirementIssue(
                            "representation_safe_tree_mismatch",
                            f"representation {retired.id} and successor {successor.id} "
                            "do not both match the declared safe-tree summary.",
                        )
                    )
                    return failure_evidence
                return {
                    "kind": "safe_tree_sha256",
                    "value": expected.tree_sha256,
                    "file_count": expected.file_count,
                    "aggregate_size_bytes": expected.aggregate_size_bytes,
                    "policy_version": expected.policy_version,
                    "policy_sha256": expected.policy_sha256,
                    "retired_summary": retired_summary,
                    "successor_summary": successor_summary,
                }
            issues.append(
                RetirementIssue(
                    "representation_content_mismatch",
                    f"representation {retired.id} and {successor.id} have different tree digests.",
                )
            )
            return {"kind": "tree_digest", "value": left or ""}
        return {"kind": "tree_digest", "value": left}
    if retired.external_url and retired.external_url == successor.external_url:
        return {
            "kind": "external_url_sha256",
            "value": hashlib.sha256(retired.external_url.encode("utf-8")).hexdigest(),
        }
    issues.append(
        RetirementIssue(
            "representation_content_unverifiable",
            f"representation {retired.id} has no matching SHA-256, tree digest, or URL.",
        )
    )
    return {"kind": "unverified", "value": ""}


def _mapping_coverage(
    *,
    kind: str,
    expected_ids: set[UUID],
    mapping: Mapping[UUID, UUID],
    issues: list[RetirementIssue],
) -> None:
    missing = sorted(expected_ids - set(mapping), key=str)
    extra = sorted(set(mapping) - expected_ids, key=str)
    if missing:
        issues.append(
            RetirementIssue(
                f"incomplete_{kind}_mapping",
                f"missing {kind} predecessors: {', '.join(map(str, missing))}",
            )
        )
    if extra:
        issues.append(
            RetirementIssue(
                f"extraneous_{kind}_mapping",
                f"unknown {kind} predecessors: {', '.join(map(str, extra))}",
            )
        )
    successors = list(mapping.values())
    if len(successors) != len(set(successors)):
        issues.append(
            RetirementIssue(
                f"duplicate_{kind}_successor",
                f"two or more {kind} predecessors map to the same successor.",
            )
        )


def preview_source_retirement(
    session: Session,
    *,
    source_id: UUID | str,
    backup_id: UUID | str,
    record_mappings: Mapping[Any, Any],
    representation_mappings: Mapping[Any, Any],
    mount_mappings: Mapping[Any, Any],
    requested_by: str,
    reason: str | None = None,
    plan_id: UUID | str | None = None,
    safe_tree_expectations: Mapping[Any, Mapping[str, Any]] | None = None,
) -> SourceRetirementPreview:
    """Build a complete signed plan without mutating the session or database."""

    retired_source_id = _uuid(source_id)
    requested_backup_id = _uuid(backup_id)
    retirement_plan_id = new_uuid7() if plan_id is None else _uuid(plan_id)
    record_map = _uuid_mapping(record_mappings)
    representation_map = _uuid_mapping(representation_mappings)
    mount_map = _uuid_mapping(mount_mappings)
    safe_tree_map = _safe_tree_expectation_mapping(safe_tree_expectations)
    issues: list[RetirementIssue] = []
    warnings: list[RetirementIssue] = []

    source = session.get(Source, retired_source_id)
    if source is None:
        raise SourceRetirementValidationError(f"source {retired_source_id} does not exist")
    frozen_manifest, frozen_config = _frozen_digests(
        source, issues, role="retired"
    )
    if not source.enabled:
        issues.append(
            RetirementIssue(
                "source_not_enabled",
                f"source {source.alias!r} is already disabled.",
            )
        )
    if is_source_retired(session, retired_source_id):
        issues.append(
            RetirementIssue(
                "source_already_retired",
                f"source {source.alias!r} already has an active retirement.",
            )
        )

    backup = session.get(BackupRun, requested_backup_id)
    if backup is None:
        issues.append(
            RetirementIssue(
                "backup_missing", f"backup {requested_backup_id} does not exist."
            )
        )
    elif backup.status != BackupStatus.VERIFIED.value:
        issues.append(
            RetirementIssue(
                "backup_not_verified",
                f"backup {requested_backup_id} has status {backup.status!r}, not 'verified'.",
            )
        )
    backup_state_digest = "" if backup is None else _state_digest(_backup_state(backup))

    retired_records = list(
        session.scalars(
            select(SourceRecord)
            .where(SourceRecord.source_id == retired_source_id)
            .order_by(SourceRecord.source_record_id, SourceRecord.id)
        )
    )
    retired_representations = list(
        session.scalars(
            select(Representation)
            .where(
                Representation.source_id == retired_source_id,
                Representation.deleted_at.is_(None),
            )
            .order_by(Representation.id)
        )
    )
    retired_representation_ids = {item.id for item in retired_representations}
    mount_conditions = [RepresentationMount.source_id == retired_source_id]
    if retired_representation_ids:
        mount_conditions.append(
            RepresentationMount.representation_id.in_(retired_representation_ids)
        )
    retired_mounts = list(
        session.scalars(
            select(RepresentationMount)
            .where(
                or_(*mount_conditions),
                RepresentationMount.deleted_at.is_(None),
            )
            .order_by(RepresentationMount.id)
        )
    )
    if not retired_records and not retired_representations and not retired_mounts:
        warnings.append(
            RetirementIssue(
                "empty_source",
                f"source {source.alias!r} has no records or active representations/mounts.",
            )
        )

    _mapping_coverage(
        kind="record",
        expected_ids={item.id for item in retired_records},
        mapping=record_map,
        issues=issues,
    )
    _mapping_coverage(
        kind="representation",
        expected_ids=retired_representation_ids,
        mapping=representation_map,
        issues=issues,
    )
    _mapping_coverage(
        kind="mount",
        expected_ids={item.id for item in retired_mounts},
        mapping=mount_map,
        issues=issues,
    )

    successor_source_ids: set[UUID] = set()
    record_payloads: list[dict[str, Any]] = []
    for retired_record in retired_records:
        successor_id = record_map.get(retired_record.id)
        successor_record = (
            None if successor_id is None else session.get(SourceRecord, successor_id)
        )
        retired_digest = _state_digest(_record_state(retired_record))
        successor_digest = ""
        if successor_id is not None and successor_record is None:
            issues.append(
                RetirementIssue(
                    "record_successor_missing",
                    f"successor source record {successor_id} does not exist.",
                )
            )
        elif successor_record is not None:
            successor_digest = _state_digest(_record_state(successor_record))
            if successor_record.source_id == retired_source_id:
                issues.append(
                    RetirementIssue(
                        "record_successor_in_retired_source",
                        f"record {retired_record.id} maps back into the retired source.",
                    )
                )
            else:
                successor_source_ids.add(successor_record.source_id)
            if successor_record.asset_id != retired_record.asset_id:
                issues.append(
                    RetirementIssue(
                        "record_asset_mismatch",
                        f"record {retired_record.id} and successor {successor_record.id} "
                        "resolve to different assets.",
                    )
                )
        record_payloads.append(
            {
                "retired_source_record_id": str(retired_record.id),
                "successor_source_record_id": (
                    None if successor_id is None else str(successor_id)
                ),
                "asset_id": (
                    None
                    if retired_record.asset_id is None
                    else str(retired_record.asset_id)
                ),
                "retired_record_digest": retired_digest,
                "successor_record_digest": successor_digest,
            }
        )

    representation_payloads: list[dict[str, Any]] = []
    for retired_representation in retired_representations:
        successor_id = representation_map.get(retired_representation.id)
        successor_representation = (
            None
            if successor_id is None
            else session.get(Representation, successor_id)
        )
        retired_digest = _state_digest(
            _representation_state(retired_representation)
        )
        successor_digest = ""
        evidence = {"kind": "missing", "value": ""}
        if successor_id is not None and successor_representation is None:
            issues.append(
                RetirementIssue(
                    "representation_successor_missing",
                    f"successor representation {successor_id} does not exist.",
                )
            )
        elif successor_representation is not None:
            successor_digest = _state_digest(
                _representation_state(successor_representation)
            )
            if successor_representation.deleted_at is not None:
                issues.append(
                    RetirementIssue(
                        "representation_successor_deleted",
                        f"successor representation {successor_representation.id} is deleted.",
                    )
                )
            if (
                successor_representation.source_id is None
                or successor_representation.source_id == retired_source_id
            ):
                issues.append(
                    RetirementIssue(
                        "representation_successor_source_invalid",
                        f"representation {retired_representation.id} has no successor "
                        "in a different source.",
                    )
                )
            else:
                successor_source_ids.add(successor_representation.source_id)
            if successor_representation.asset_id != retired_representation.asset_id:
                issues.append(
                    RetirementIssue(
                        "representation_asset_mismatch",
                        f"representation {retired_representation.id} and successor "
                        f"{successor_representation.id} "
                        "belong to different assets.",
                    )
                )
            expected_record = (
                None
                if retired_representation.source_record_id is None
                else record_map.get(retired_representation.source_record_id)
            )
            if (
                expected_record is not None
                and successor_representation.source_record_id != expected_record
            ):
                issues.append(
                    RetirementIssue(
                        "representation_record_mismatch",
                        f"representation {retired_representation.id} and successor "
                        f"{successor_representation.id} are not bound to their mapped "
                        "source records.",
                    )
                )
            if (
                successor_representation.representation_type
                != retired_representation.representation_type
                or successor_representation.role != retired_representation.role
                or successor_representation.materialization_kind
                != retired_representation.materialization_kind
            ):
                issues.append(
                    RetirementIssue(
                        "representation_semantics_mismatch",
                        f"representation {retired_representation.id} and successor "
                        f"{successor_representation.id} "
                        "have different type/role/materialization.",
                    )
                )
            evidence = _content_evidence(
                session,
                retired_representation,
                successor_representation,
                issues,
                safe_tree_expectation=safe_tree_map.get(
                    retired_representation.id
                ),
            )
        representation_payloads.append(
            {
                "retired_representation_id": str(retired_representation.id),
                "successor_representation_id": (
                    None if successor_id is None else str(successor_id)
                ),
                "asset_id": str(retired_representation.asset_id),
                "content_evidence": evidence,
                "retired_representation_digest": retired_digest,
                "successor_representation_digest": successor_digest,
            }
        )

    unused_safe_tree_expectations = set(safe_tree_map) - retired_representation_ids
    if unused_safe_tree_expectations:
        issues.append(
            RetirementIssue(
                "extraneous_safe_tree_expectation",
                "safe-tree expectations reference representations outside the retired source: "
                + ", ".join(
                    map(str, sorted(unused_safe_tree_expectations, key=str))
                ),
            )
        )

    mount_payloads: list[dict[str, Any]] = []
    for retired_mount in retired_mounts:
        successor_id = mount_map.get(retired_mount.id)
        successor_mount = (
            None
            if successor_id is None
            else session.get(RepresentationMount, successor_id)
        )
        retired_digest = _state_digest(_mount_state(retired_mount))
        successor_digest = ""
        successor_representation_id: UUID | None = None
        if successor_id is not None and successor_mount is None:
            issues.append(
                RetirementIssue(
                    "mount_successor_missing",
                    f"successor mount {successor_id} does not exist.",
                )
            )
        elif successor_mount is not None:
            successor_representation_id = successor_mount.representation_id
            successor_digest = _state_digest(_mount_state(successor_mount))
            if successor_mount.deleted_at is not None:
                issues.append(
                    RetirementIssue(
                        "mount_successor_deleted",
                        f"successor mount {successor_mount.id} is deleted.",
                    )
                )
            if successor_mount.source_id == retired_source_id:
                issues.append(
                    RetirementIssue(
                        "mount_successor_in_retired_source",
                        f"mount {retired_mount.id} maps back into the retired source.",
                    )
                )
            else:
                successor_source_ids.add(successor_mount.source_id)
            expected_representation = representation_map.get(
                retired_mount.representation_id
            )
            if (
                expected_representation is not None
                and successor_mount.representation_id != expected_representation
            ):
                issues.append(
                    RetirementIssue(
                        "mount_representation_mismatch",
                        f"mount {retired_mount.id} does not target its mapped "
                        "successor representation.",
                    )
                )
            if successor_mount.mount_path != retired_mount.mount_path:
                issues.append(
                    RetirementIssue(
                        "mount_path_mismatch",
                        f"mount {retired_mount.id} and successor {successor_mount.id} "
                        "expose different virtual paths.",
                    )
                )
            if expected_representation is None:
                retired_mounted_representation = session.get(
                    Representation, retired_mount.representation_id
                )
                successor_mounted_representation = session.get(
                    Representation, successor_mount.representation_id
                )
                if (
                    retired_mounted_representation is None
                    or successor_mounted_representation is None
                    or retired_mounted_representation.asset_id
                    != successor_mounted_representation.asset_id
                ):
                    issues.append(
                        RetirementIssue(
                            "mount_asset_mismatch",
                            f"mount {retired_mount.id} and successor {successor_mount.id} "
                            "do not resolve to the same asset.",
                        )
                    )
        mount_payloads.append(
            {
                "retired_mount_id": str(retired_mount.id),
                "successor_mount_id": None if successor_id is None else str(successor_id),
                "retired_representation_id": str(retired_mount.representation_id),
                "successor_representation_id": (
                    None
                    if successor_representation_id is None
                    else str(successor_representation_id)
                ),
                "retired_mount_digest": retired_digest,
                "successor_mount_digest": successor_digest,
            }
        )

    successor_payloads: list[dict[str, Any]] = []
    for successor_source_id in sorted(successor_source_ids, key=str):
        successor_source = session.get(Source, successor_source_id)
        if successor_source is None:
            issues.append(
                RetirementIssue(
                    "successor_source_missing",
                    f"successor source {successor_source_id} does not exist.",
                )
            )
            continue
        successor_manifest, successor_config = _frozen_digests(
            successor_source, issues, role="successor"
        )
        if not successor_source.enabled:
            issues.append(
                RetirementIssue(
                    "successor_source_disabled",
                    f"successor source {successor_source.alias!r} is disabled.",
                )
            )
        successor_payloads.append(
            {
                "source_id": str(successor_source.id),
                "alias": successor_source.alias,
                "frozen_manifest_sha256": successor_manifest,
                "frozen_source_config_sha256": successor_config,
                "source_state_digest": _state_digest(_source_state(successor_source)),
            }
        )

    record_states = [_record_state(item) for item in retired_records]
    representation_states = [
        _representation_state(item) for item in retired_representations
    ]
    mount_states = [_mount_state(item) for item in retired_mounts]
    record_set_digest = canonical_payload_digest({"records": record_states})
    representation_set_digest = canonical_payload_digest(
        {"representations": representation_states}
    )
    mount_set_digest = canonical_payload_digest({"mounts": mount_states})
    collection_digest = canonical_payload_digest(
        {
            "record_set_digest": record_set_digest,
            "representation_set_digest": representation_set_digest,
            "mount_set_digest": mount_set_digest,
        }
    )
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "plan_id": str(retirement_plan_id),
        "source_id": str(retired_source_id),
        "source_alias": source.alias,
        "backup_id": str(requested_backup_id),
        "backup_state_digest": backup_state_digest,
        "requested_by": requested_by,
        "reason": reason,
        "frozen_manifest_sha256": frozen_manifest,
        "frozen_source_config_sha256": frozen_config,
        "record_set_digest": record_set_digest,
        "representation_set_digest": representation_set_digest,
        "mount_set_digest": mount_set_digest,
        "collection_digest": collection_digest,
        "successors": successor_payloads,
        "record_mappings": record_payloads,
        "representation_mappings": representation_payloads,
        "mount_mappings": mount_payloads,
    }
    return SourceRetirementPreview(
        payload=payload,
        payload_digest=canonical_payload_digest(payload),
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def _verify_signature(
    *,
    payload: Mapping[str, Any],
    signature: bytes,
    signer_key_id: str,
    signature_algorithm: str,
    signature_verifier: SignatureVerifier,
) -> None:
    if not signature or not signer_key_id or not signature_algorithm:
        raise SourceRetirementSignatureError("signature metadata must not be empty")
    try:
        valid = signature_verifier(
            payload=canonical_payload_bytes(payload),
            signature=signature,
            key_id=signer_key_id,
            algorithm=signature_algorithm,
        )
    except Exception as error:
        raise SourceRetirementSignatureError(
            f"signature verifier rejected payload: {type(error).__name__}"
        ) from error
    if not valid:
        raise SourceRetirementSignatureError("signature verifier rejected payload")


def _require_fresh_transaction(session: Session) -> str:
    if session.in_transaction():
        raise SourceRetirementTransactionError(
            "apply/rollback require a fresh Session so SERIALIZABLE covers the whole operation"
        )
    connection = session.connection(
        execution_options={"isolation_level": "SERIALIZABLE"}
    )
    dialect = connection.dialect.name
    if dialect == "sqlite":
        # SQLite's default deferred BEGIN permits two writers to validate the
        # same absence before either writes.  IMMEDIATE serializes the whole
        # validate-and-append operation at the database boundary.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    return dialect


def _advisory_lock_sources(
    session: Session,
    *,
    dialect: str,
    source_ids: Sequence[UUID],
) -> None:
    if dialect != "postgresql":
        return
    for source_id in sorted(set(source_ids), key=str):
        lock_key = int.from_bytes(
            hashlib.sha256(source_id.bytes).digest()[:8], "big", signed=False
        ) & ((1 << 63) - 1)
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


def _mapping_from_payload(
    payload: Mapping[str, Any],
    collection: str,
    retired_key: str,
    successor_key: str,
) -> dict[UUID, UUID]:
    result: dict[UUID, UUID] = {}
    rows = payload.get(collection)
    if not isinstance(rows, list):
        raise SourceRetirementValidationError(f"{collection} must be a list")
    for row in rows:
        if not isinstance(row, dict):
            raise SourceRetirementValidationError(f"{collection} entries must be objects")
        retired_value = row.get(retired_key)
        successor_value = row.get(successor_key)
        if retired_value is None or successor_value is None:
            raise SourceRetirementValidationError(
                f"{collection} contains an incomplete mapping"
            )
        retired_id = _uuid(str(retired_value))
        if retired_id in result:
            raise SourceRetirementValidationError(
                f"{collection} contains duplicate predecessor {retired_id}"
            )
        result[retired_id] = _uuid(str(successor_value))
    return result


def _safe_tree_expectations_from_payload(
    payload: Mapping[str, Any],
) -> dict[UUID, dict[str, Any]]:
    """Recover signed safe-tree declarations so apply re-scans both directories."""

    rows = payload.get("representation_mappings")
    if not isinstance(rows, list):
        raise SourceRetirementValidationError("representation_mappings must be a list")
    result: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SourceRetirementValidationError(
                "representation_mappings entries must be objects"
            )
        evidence = row.get("content_evidence")
        if not isinstance(evidence, Mapping):
            continue
        if evidence.get("kind") != "safe_tree_sha256":
            continue
        retired_id = _uuid(str(row["retired_representation_id"]))
        if retired_id in result:
            raise SourceRetirementValidationError(
                f"duplicate safe-tree payload evidence for representation {retired_id}"
            )
        retired_summary = evidence.get("retired_summary")
        successor_summary = evidence.get("successor_summary")
        declared_summary = {
            "tree_sha256": evidence.get("value"),
            "file_count": evidence.get("file_count"),
            "aggregate_size_bytes": evidence.get("aggregate_size_bytes"),
        }
        if retired_summary != declared_summary or successor_summary != declared_summary:
            raise SourceRetirementValidationError(
                "safe-tree payload summaries are internally inconsistent"
            )
        result[retired_id] = {
            "successor_representation_id": row.get(
                "successor_representation_id"
            ),
            **declared_summary,
            "policy_version": evidence.get("policy_version"),
            "policy_sha256": evidence.get("policy_sha256"),
        }
    return result


def _regenerate_preview(session: Session, payload: Mapping[str, Any]) -> SourceRetirementPreview:
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise SourceRetirementValidationError("unsupported retirement payload schema")
    return preview_source_retirement(
        session,
        source_id=str(payload["source_id"]),
        backup_id=str(payload["backup_id"]),
        record_mappings=_mapping_from_payload(
            payload,
            "record_mappings",
            "retired_source_record_id",
            "successor_source_record_id",
        ),
        representation_mappings=_mapping_from_payload(
            payload,
            "representation_mappings",
            "retired_representation_id",
            "successor_representation_id",
        ),
        mount_mappings=_mapping_from_payload(
            payload,
            "mount_mappings",
            "retired_mount_id",
            "successor_mount_id",
        ),
        requested_by=str(payload["requested_by"]),
        reason=payload.get("reason"),
        plan_id=str(payload["plan_id"]),
        safe_tree_expectations=_safe_tree_expectations_from_payload(payload),
    )


def _validate_signed_preview(
    signed: SignedSourceRetirementPreview,
    signature_verifier: SignatureVerifier,
) -> tuple[UUID, str]:
    preview = signed.preview
    actual_digest = canonical_payload_digest(preview.payload)
    if preview.payload_digest != actual_digest:
        raise SourceRetirementDriftError("preview payload digest is inconsistent")
    if not preview.ready:
        codes = ", ".join(item.code for item in preview.issues)
        raise SourceRetirementValidationError(f"retirement preview is invalid: {codes}")
    _verify_signature(
        payload=preview.payload,
        signature=signed.signature,
        signer_key_id=signed.signer_key_id,
        signature_algorithm=signed.signature_algorithm,
        signature_verifier=signature_verifier,
    )
    return _uuid(str(preview.payload["source_id"])), actual_digest


def _append_source_retirement(
    session: Session,
    *,
    source: Source,
    signed: SignedSourceRetirementPreview,
    payload_digest: str,
) -> SourceRetirement:
    payload = signed.preview.payload
    retirement = SourceRetirement(
        id=_uuid(str(payload["plan_id"])),
        source_id=source.id,
        backup_id=_uuid(str(payload["backup_id"])),
        payload=dict(payload),
        payload_digest=payload_digest,
        frozen_manifest_sha256=str(payload["frozen_manifest_sha256"]),
        frozen_source_config_sha256=str(payload["frozen_source_config_sha256"]),
        record_set_digest=str(payload["record_set_digest"]),
        representation_set_digest=str(payload["representation_set_digest"]),
        mount_set_digest=str(payload["mount_set_digest"]),
        collection_digest=str(payload["collection_digest"]),
        signature_algorithm=signed.signature_algorithm,
        signer_key_id=signed.signer_key_id,
        signature=base64.b64encode(signed.signature).decode("ascii"),
        reason=payload.get("reason"),
        applied_by=str(payload["requested_by"]),
    )
    session.add(retirement)
    for row in payload["record_mappings"]:
        session.add(
            SourceRetirementRecordMap(
                retirement_id=retirement.id,
                retired_source_record_id=_uuid(row["retired_source_record_id"]),
                successor_source_record_id=_uuid(row["successor_source_record_id"]),
                asset_id=None if row["asset_id"] is None else _uuid(row["asset_id"]),
                retired_record_digest=row["retired_record_digest"],
                successor_record_digest=row["successor_record_digest"],
            )
        )
    for row in payload["representation_mappings"]:
        session.add(
            SourceRetirementRepresentationMap(
                retirement_id=retirement.id,
                retired_representation_id=_uuid(row["retired_representation_id"]),
                successor_representation_id=_uuid(row["successor_representation_id"]),
                asset_id=_uuid(row["asset_id"]),
                content_digest=row["content_evidence"]["value"],
                retired_representation_digest=row["retired_representation_digest"],
                successor_representation_digest=row[
                    "successor_representation_digest"
                ],
            )
        )
    for row in payload["mount_mappings"]:
        session.add(
            SourceRetirementMountMap(
                retirement_id=retirement.id,
                retired_mount_id=_uuid(row["retired_mount_id"]),
                successor_mount_id=_uuid(row["successor_mount_id"]),
                retired_representation_id=_uuid(row["retired_representation_id"]),
                successor_representation_id=_uuid(
                    row["successor_representation_id"]
                ),
                retired_mount_digest=row["retired_mount_digest"],
                successor_mount_digest=row["successor_mount_digest"],
            )
        )
    for successor in payload["successors"]:
        session.add(
            SourceRetirementSuccessor(
                retirement_id=retirement.id,
                successor_source_id=_uuid(successor["source_id"]),
                frozen_manifest_sha256=successor["frozen_manifest_sha256"],
                frozen_source_config_sha256=successor[
                    "frozen_source_config_sha256"
                ],
                source_state_digest=successor["source_state_digest"],
            )
        )
    source.enabled = False
    return retirement


def apply_source_retirement_batch(
    session: Session,
    *,
    signed_previews: Sequence[SignedSourceRetirementPreview],
    signature_verifier: SignatureVerifier,
) -> dict[str, Any]:
    """Apply an all-or-nothing batch of signed source retirements.

    Every signature and canonical digest is checked before the transaction.
    Inside one SERIALIZABLE transaction, all retired and successor sources are
    locked, every preview is regenerated, and only then are any events added.
    A mixed state (some plans already applied, some absent) fails closed.
    """

    if not signed_previews:
        raise SourceRetirementValidationError("retirement batch must not be empty")
    validated = [
        (*_validate_signed_preview(signed, signature_verifier), signed)
        for signed in signed_previews
    ]
    retired_source_ids = [source_id for source_id, _, _ in validated]
    if len(retired_source_ids) != len(set(retired_source_ids)):
        raise SourceRetirementValidationError(
            "retirement batch contains the same source more than once"
        )
    successor_source_ids = {
        _uuid(str(row["source_id"]))
        for _, _, signed in validated
        for row in signed.preview.payload["successors"]
    }
    overlap = set(retired_source_ids) & successor_source_ids
    if overlap:
        raise SourceRetirementValidationError(
            "a successor source cannot be retired in the same batch: "
            + ", ".join(map(str, sorted(overlap, key=str)))
        )

    dialect = _require_fresh_transaction(session)
    try:
        _advisory_lock_sources(
            session,
            dialect=dialect,
            source_ids=retired_source_ids,
        )
        locked_source_ids = sorted(
            set(retired_source_ids) | successor_source_ids, key=str
        )
        locked_sources = {
            source.id: source
            for source in session.scalars(
                select(Source)
                .where(Source.id.in_(locked_source_ids))
                .order_by(Source.id)
                .with_for_update()
            )
        }
        missing_sources = set(locked_source_ids) - set(locked_sources)
        if missing_sources:
            raise SourceRetirementValidationError(
                "retirement batch references missing sources: "
                + ", ".join(map(str, sorted(missing_sources, key=str)))
            )

        prepared: list[
            tuple[
                Source,
                SignedSourceRetirementPreview,
                str,
                SourceRetirement | None,
            ]
        ] = []
        for source_id, payload_digest, signed in validated:
            source = locked_sources[source_id]
            existing = session.scalar(
                select(SourceRetirement).where(
                    SourceRetirement.payload_digest == payload_digest
                )
            )
            if existing is not None:
                if existing.source_id != source_id:
                    raise SourceRetirementDriftError(
                        "retirement payload digest is bound to a different source"
                    )
                rolled_back = session.scalar(
                    select(SourceRetirementRollback.id).where(
                        SourceRetirementRollback.retirement_id == existing.id
                    )
                )
                if rolled_back is not None:
                    raise SourceRetirementValidationError(
                        "an identical retirement plan was already rolled back"
                    )
                if source.enabled:
                    raise SourceRetirementDriftError(
                        "retirement exists but its source is unexpectedly enabled"
                    )
                prepared.append((source, signed, payload_digest, existing))
                continue

            if bool(session.scalar(select(active_retirement_exists(source_id)))):
                raise SourceRetirementValidationError(
                    f"source {source.alias!r} already has a different active retirement"
                )
            regenerated = _regenerate_preview(session, signed.preview.payload)
            if not regenerated.ready:
                codes = ", ".join(item.code for item in regenerated.issues)
                raise SourceRetirementDriftError(
                    f"retirement state changed after preview: {codes}"
                )
            if (
                regenerated.payload_digest != payload_digest
                or regenerated.payload != signed.preview.payload
            ):
                raise SourceRetirementDriftError(
                    "retirement records, representations, mounts, sources, or backup drifted"
                )
            prepared.append((source, signed, payload_digest, None))

        existing_count = sum(existing is not None for *_, existing in prepared)
        if existing_count not in {0, len(prepared)}:
            raise SourceRetirementValidationError(
                "retirement batch is partially applied; refusing to create a mixed state"
            )
        if existing_count == len(prepared):
            session.commit()
            return {
                "status": "unchanged",
                "idempotent": True,
                "count": len(prepared),
                "items": [
                    {
                        "status": "unchanged",
                        "idempotent": True,
                        "retirement_id": str(existing.id),
                        "source_id": str(source.id),
                        "payload_digest": payload_digest,
                    }
                    for source, _, payload_digest, existing in prepared
                    if existing is not None
                ],
            }

        retirements: list[SourceRetirement] = []
        for source, signed, payload_digest, _ in prepared:
            retirements.append(
                _append_source_retirement(
                    session,
                    source=source,
                    signed=signed,
                    payload_digest=payload_digest,
                )
            )
        session.flush()
        items = [
            {
                "status": "applied",
                "idempotent": False,
                "retirement_id": str(retirement.id),
                "source_id": str(source.id),
                "payload_digest": payload_digest,
                "counts": {
                    "records": len(signed.preview.payload["record_mappings"]),
                    "representations": len(
                        signed.preview.payload["representation_mappings"]
                    ),
                    "mounts": len(signed.preview.payload["mount_mappings"]),
                    "successor_sources": len(
                        signed.preview.payload["successors"]
                    ),
                },
            }
            for retirement, (source, signed, payload_digest, _) in zip(
                retirements, prepared, strict=True
            )
        ]
        session.commit()
        return {
            "status": "applied",
            "idempotent": False,
            "count": len(items),
            "items": items,
        }
    except Exception:
        session.rollback()
        raise


def apply_source_retirement(
    session: Session,
    *,
    preview: SourceRetirementPreview,
    signature: bytes,
    signer_key_id: str,
    signature_verifier: SignatureVerifier,
    signature_algorithm: str = "ed25519",
) -> dict[str, Any]:
    """Backward-compatible single-source wrapper around the atomic batch path."""

    report = apply_source_retirement_batch(
        session,
        signed_previews=[
            SignedSourceRetirementPreview(
                preview=preview,
                signature=signature,
                signer_key_id=signer_key_id,
                signature_algorithm=signature_algorithm,
            )
        ],
        signature_verifier=signature_verifier,
    )
    return dict(report["items"][0])


def preview_source_retirement_rollback(
    session: Session,
    *,
    retirement_id: UUID | str,
    reason: str,
    requested_by: str,
) -> dict[str, Any]:
    """Return the canonical rollback payload that must be signed."""

    if not reason.strip():
        raise SourceRetirementValidationError("rollback reason must not be empty")
    retirement = session.get(SourceRetirement, _uuid(retirement_id))
    if retirement is None:
        raise SourceRetirementValidationError(
            f"retirement {retirement_id} does not exist"
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "retirement_id": str(retirement.id),
        "retirement_payload_digest": retirement.payload_digest,
        "source_id": str(retirement.source_id),
        "backup_id": str(retirement.backup_id),
        "requested_by": requested_by,
        "reason": reason,
    }


def rollback_source_retirement(
    session: Session,
    *,
    payload: Mapping[str, Any],
    signature: bytes,
    signer_key_id: str,
    signature_verifier: SignatureVerifier,
    signature_algorithm: str = "ed25519",
) -> dict[str, Any]:
    """Append a signed rollback event and atomically re-enable the source."""

    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise SourceRetirementValidationError("unsupported rollback payload schema")
    rollback_payload = dict(payload)
    rollback_digest = canonical_payload_digest(rollback_payload)
    _verify_signature(
        payload=rollback_payload,
        signature=signature,
        signer_key_id=signer_key_id,
        signature_algorithm=signature_algorithm,
        signature_verifier=signature_verifier,
    )
    _require_fresh_transaction(session)
    try:
        retirement_id = _uuid(str(rollback_payload["retirement_id"]))
        retirement = session.scalar(
            select(SourceRetirement)
            .where(SourceRetirement.id == retirement_id)
            .with_for_update()
        )
        if retirement is None:
            raise SourceRetirementValidationError(
                f"retirement {retirement_id} does not exist"
            )
        source = session.scalar(
            select(Source)
            .where(Source.id == retirement.source_id)
            .with_for_update()
        )
        if source is None:
            raise SourceRetirementValidationError(
                f"source {retirement.source_id} does not exist"
            )
        expected = {
            "schema_version": _SCHEMA_VERSION,
            "retirement_id": str(retirement.id),
            "retirement_payload_digest": retirement.payload_digest,
            "source_id": str(retirement.source_id),
            "backup_id": str(retirement.backup_id),
            "requested_by": str(rollback_payload["requested_by"]),
            "reason": str(rollback_payload["reason"]),
        }
        if expected != rollback_payload:
            raise SourceRetirementDriftError(
                "rollback payload does not match the immutable retirement"
            )
        if not str(rollback_payload["reason"]).strip():
            raise SourceRetirementValidationError("rollback reason must not be empty")

        existing = session.scalar(
            select(SourceRetirementRollback).where(
                SourceRetirementRollback.retirement_id == retirement.id
            )
        )
        if existing is not None:
            if existing.payload_digest != rollback_digest:
                raise SourceRetirementValidationError(
                    "retirement already has a different rollback event"
                )
            if not source.enabled:
                raise SourceRetirementDriftError(
                    "rollback exists but its source is unexpectedly disabled"
                )
            session.commit()
            return {
                "status": "unchanged",
                "idempotent": True,
                "rollback_id": str(existing.id),
                "retirement_id": str(retirement.id),
                "source_id": str(source.id),
                "payload_digest": rollback_digest,
            }
        if source.enabled:
            raise SourceRetirementDriftError(
                "active retirement source is unexpectedly enabled"
            )
        backup = session.get(BackupRun, retirement.backup_id)
        if backup is None or backup.status != BackupStatus.VERIFIED.value:
            raise SourceRetirementValidationError(
                "the retirement backup is missing or no longer verified"
            )
        rollback = SourceRetirementRollback(
            retirement_id=retirement.id,
            backup_id=retirement.backup_id,
            payload=rollback_payload,
            payload_digest=rollback_digest,
            signature_algorithm=signature_algorithm,
            signer_key_id=signer_key_id,
            signature=base64.b64encode(signature).decode("ascii"),
            reason=str(rollback_payload["reason"]),
            rolled_back_by=str(rollback_payload["requested_by"]),
        )
        session.add(rollback)
        source.enabled = True
        session.flush()
        rollback_id = rollback.id
        session.commit()
        return {
            "status": "rolled_back",
            "idempotent": False,
            "rollback_id": str(rollback_id),
            "retirement_id": str(retirement.id),
            "source_id": str(source.id),
            "payload_digest": rollback_digest,
        }
    except Exception:
        session.rollback()
        raise


__all__ = [
    "SignedSourceRetirementPreview",
    "SignatureVerifier",
    "SourceRetirementDriftError",
    "SourceRetirementError",
    "SourceRetirementPreview",
    "SourceRetirementSignatureError",
    "SourceRetirementTransactionError",
    "SourceRetirementValidationError",
    "active_retirement_exists",
    "apply_source_retirement",
    "apply_source_retirement_batch",
    "canonical_payload_bytes",
    "canonical_payload_digest",
    "is_source_retired",
    "preview_source_retirement",
    "preview_source_retirement_rollback",
    "rollback_source_retirement",
]
