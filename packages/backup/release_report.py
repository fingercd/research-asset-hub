"""Privacy-safe release, migration and failure reporting.

The operational PostgreSQL database is the source of truth. Rebuildable search
services and expensive browser/model checks are represented by their existing
machine-readable validation reports. This module intentionally projects only
counts, stable identifiers and registered relative paths; it never copies
private document text, credentials, connection strings or source-root paths.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from packages.domain.models import (
    Annotation,
    Asset,
    AuditEvent,
    ReadingState,
    Relation,
    Representation,
    Source,
    SourceRecord,
)
from packages.domain.platform_models import (
    Activity,
    AnnotationRevision,
    BackupRun,
    DocumentChunk,
    IndexState,
    Integration,
    Job,
    OutboxEvent,
    RelationEvidenceModel,
    SourceRecordAsset,
    SourceRecordVersion,
    SyncConflict,
    SyncRun,
    Tombstone,
)

GateStatus = Literal["pass", "warn", "fail"]
_FORMAL_IMPORT_SCHEMA_VERSION = 2
_FORMAL_IMPORT_IDEMPOTENCY_COUNTERS = (
    "source_records_created",
    "source_record_versions_created",
    "paper_assets_created",
    "repository_assets_created",
    "representations_created",
    "relations_created",
    "relation_evidence_created",
    "field_claims_created",
)
_FORMAL_IMPORT_ACCOUNTING_COUNTERS = (
    "relation_candidates",
    "relation_candidates_resolved",
    "relation_candidates_unresolved",
    "representation_candidates",
    "representations_available",
    "representations_missing",
)
_FORMAL_IMPORT_REQUIRED_COUNTERS = frozenset(
    (
        *_FORMAL_IMPORT_IDEMPOTENCY_COUNTERS,
        *_FORMAL_IMPORT_ACCOUNTING_COUNTERS,
        "unapproved_merge_groups",
    )
)
_REDACTED_KEYS = frozenset(
    {
        "abstract",
        "after",
        "api_key",
        "base_value",
        "before",
        "body_markdown",
        "config",
        "content",
        "database_url",
        "dsn",
        "error",
        "external_value",
        "hub_value",
        "message",
        "parameters",
        "password",
        "payload",
        "raw_data",
        "resolved_value",
        "root_path",
        "secret",
        "source_fields",
        "text_content",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class LegacyReference:
    """The accepted six-domain v1 scope, used as a floor rather than a ceiling."""

    domain_count: int = 6
    source_records: int = 428
    paper_assets: int = 411
    repository_assets: int = 266
    representation_candidates: int = 2161
    known_missing_representations: int = 9
    relation_candidates: int = 1753
    implemented_by_relations: int = 279
    implemented_by_evidence: int = 286


@dataclass(frozen=True, slots=True)
class ReleaseReportInputs:
    """Pure-data input, making gate logic testable without PostgreSQL."""

    database: Mapping[str, Any]
    reports: Mapping[str, Mapping[str, Any]]
    generated_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _count_by(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _scalar_count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _schema_revision(session: Session) -> str | None:
    try:
        value = session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return None
    return str(value) if value is not None else None


def _safe_registered_path(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace("\\", "/")
    if PureWindowsPath(value).is_absolute() or PurePosixPath(normalized).is_absolute():
        name = PureWindowsPath(value).name or PurePosixPath(normalized).name
        return f"<absolute-path-redacted>/{name}"
    return normalized


def _privacy_safe_value(value: Any, *, key: str | None = None) -> Any:
    """Defensively redact sensitive keys and absolute paths from pure inputs."""

    if key is not None and key.casefold() in _REDACTED_KEYS:
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _privacy_safe_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_privacy_safe_value(item) for item in value]
    if isinstance(value, str):
        lowered = value.casefold()
        if lowered.startswith(("postgresql://", "postgres://")):
            return "<database-url-redacted>"
        path_key = bool(
            key
            and (
                key.casefold().endswith("_path")
                or key.casefold().endswith("_root")
                or key.casefold() in {"path", "path_ref", "entrypoint"}
            )
        )
        if PureWindowsPath(value).is_absolute() or (
            path_key
            and PurePosixPath(value.replace("\\", "/")).is_absolute()
        ):
            return _safe_registered_path(value)
        return value[:4000]
    return value


def _safe_error_code(value: object) -> str:
    if not isinstance(value, Mapping):
        return "unspecified"
    for key in ("code", "type", "error_type", "class", "status"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int, float, bool)) and str(candidate).strip():
            return str(candidate)[:127]
    return "unspecified"


def _safe_parameter_ids(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)[:128]
        for key, item in value.items()
        if str(key).endswith("_id") and isinstance(item, (str, int))
    }


def _grouped_count(
    session: Session,
    model: type[Any],
    column: Any,
) -> dict[str, int]:
    rows = session.execute(
        select(column, func.count()).select_from(model).group_by(column)
    ).all()
    return {
        str(key): int(count)
        for key, count in sorted(rows, key=lambda row: str(row[0]))
    }


def collect_database_snapshot(session: Session) -> dict[str, Any]:
    """Collect a privacy-safe operational snapshot from PostgreSQL or SQLite."""

    assets = session.execute(
        select(Asset.id, Asset.asset_type, Asset.domain_slugs, Asset.deleted_at)
    ).all()
    active_assets = [row for row in assets if row.deleted_at is None]
    all_asset_ids = {row.id for row in assets}
    active_asset_ids = {row.id for row in active_assets}
    domains = Counter(
        str(domain)
        for row in active_assets
        for domain in (row.domain_slugs or [])
    )

    sources = session.execute(
        select(
            Source.alias,
            Source.read_only,
            Source.enabled,
            Source.config,
        ).order_by(Source.alias)
    ).all()
    representations = session.execute(
        select(
            Representation.id,
            Representation.asset_id,
            Representation.representation_type,
            Representation.availability,
            Representation.network_required,
            Representation.root_key,
            Representation.path_ref,
            Representation.mime_type,
            Representation.deleted_at,
        ).order_by(Representation.id)
    ).all()
    active_representations = [row for row in representations if row.deleted_at is None]
    missing_representations = [
        {
            "representation_id": str(row.id),
            "asset_id": str(row.asset_id),
            "type": row.representation_type,
            "root_key": row.root_key,
            "path_ref": _safe_registered_path(row.path_ref),
        }
        for row in active_representations
        if row.availability == "missing"
    ]
    html_suffixes = {".htm", ".html", ".xhtml"}
    html_candidates = sum(
        (
            PurePosixPath((row.path_ref or "").replace("\\", "/")).suffix.casefold()
            in html_suffixes
        )
        or (
            (row.mime_type or "").split(";", 1)[0].casefold()
            in {"text/html", "application/xhtml+xml"}
        )
        for row in active_representations
        if row.representation_type in {"html", "html_document", "analysis", "guide"}
    )

    relations = session.execute(
        select(
            Relation.id,
            Relation.subject_asset_id,
            Relation.object_asset_id,
            Relation.relation_type,
            Relation.confirmation_status,
            Relation.deleted_at,
        )
    ).all()
    active_relations = [row for row in relations if row.deleted_at is None]
    relation_keys = Counter(
        (row.subject_asset_id, row.relation_type, row.object_asset_id)
        for row in active_relations
    )
    duplicate_relations = sum(count - 1 for count in relation_keys.values() if count > 1)
    dangling_relations = sum(
        row.subject_asset_id not in all_asset_ids or row.object_asset_id not in all_asset_ids
        for row in active_relations
    )
    inactive_endpoint_relations = sum(
        row.subject_asset_id not in active_asset_ids
        or row.object_asset_id not in active_asset_ids
        for row in active_relations
    )
    evidence_rows = session.execute(
        select(
            RelationEvidenceModel.relation_id,
            RelationEvidenceModel.provider,
        )
    ).all()
    active_relation_ids = {row.id for row in active_relations}

    jobs = session.execute(
        select(
            Job.id,
            Job.job_type,
            Job.status,
            Job.attempts,
            Job.max_attempts,
            Job.progress,
            Job.parameters,
            Job.error,
        ).order_by(Job.id)
    ).all()
    failed_jobs = [
        {
            "job_id": str(row.id),
            "job_type": row.job_type,
            "status": row.status,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "entity_ids": _safe_parameter_ids(row.parameters),
            "error_code": _safe_error_code(row.error),
        }
        for row in jobs
        if row.status == "failed"
    ]

    activities = session.execute(
        select(
            Activity.id,
            Activity.asset_id,
            Activity.representation_id,
            Activity.activity_type,
            Activity.tool_name,
            Activity.tool_version,
            Activity.status,
            Activity.result,
        )
    ).all()
    ocr_states: Counter[str] = Counter()
    extraction_outcomes: Counter[str] = Counter()
    failed_activities: list[dict[str, Any]] = []
    for row in activities:
        if row.activity_type == "extraction":
            result = row.result if isinstance(row.result, Mapping) else {}
            extraction_outcomes[str(result.get("status") or row.status)] += 1
            raw_ocr_states = result.get("ocr_states")
            if isinstance(raw_ocr_states, Sequence) and not isinstance(
                raw_ocr_states, (str, bytes)
            ):
                ocr_states.update(str(value) for value in raw_ocr_states)
        if row.status == "failed":
            failed_activities.append(
                {
                    "activity_id": str(row.id),
                    "asset_id": str(row.asset_id) if row.asset_id else None,
                    "representation_id": (
                        str(row.representation_id) if row.representation_id else None
                    ),
                    "activity_type": row.activity_type,
                    "tool": row.tool_name,
                    "tool_version": row.tool_version,
                }
            )

    index_states = session.execute(
        select(
            IndexState.id,
            IndexState.index_name,
            IndexState.index_kind,
            IndexState.entity_type,
            IndexState.status,
            IndexState.provider,
            IndexState.model,
            IndexState.dimension,
            IndexState.version,
        )
    ).all()
    failed_index_states = [
        {
            "index_state_id": str(row.id),
            "index_name": row.index_name,
            "index_kind": row.index_kind,
            "entity_type": row.entity_type,
            "version": row.version,
        }
        for row in index_states
        if row.status == "failed"
    ]
    index_versions: dict[str, dict[str, Any]] = {}
    for row in index_states:
        key = f"{row.index_name}:{row.version}"
        item = index_versions.setdefault(
            key,
            {
                "index_name": row.index_name,
                "version": row.version,
                "kind": row.index_kind,
                "provider": row.provider,
                "model": row.model,
                "dimension": row.dimension,
                "statuses": Counter(),
            },
        )
        statuses = item["statuses"]
        if isinstance(statuses, Counter):
            statuses[row.status] += 1
    for item in index_versions.values():
        statuses = item["statuses"]
        if isinstance(statuses, Counter):
            item["statuses"] = dict(sorted(statuses.items()))

    integrations = session.execute(
        select(
            Integration.id,
            Integration.provider,
            Integration.name,
            Integration.enabled,
            Integration.status,
        ).order_by(Integration.provider, Integration.name)
    ).all()
    sync_conflicts = session.execute(
        select(
            SyncConflict.id,
            SyncConflict.entity_type,
            SyncConflict.entity_id,
            SyncConflict.field_path,
            SyncConflict.status,
        ).order_by(SyncConflict.id)
    ).all()
    pending_conflicts = [
        {
            "conflict_id": str(row.id),
            "entity_type": row.entity_type,
            "entity_id": str(row.entity_id),
            "field_path": row.field_path,
        }
        for row in sync_conflicts
        if row.status == "pending"
    ]
    backup_runs = session.execute(
        select(
            BackupRun.id,
            BackupRun.status,
            BackupRun.backup_kind,
            BackupRun.local_copy_only,
            BackupRun.verified_at,
            BackupRun.restored_at,
        ).order_by(BackupRun.created_at)
    ).all()

    audit_by_action = _grouped_count(session, AuditEvent, AuditEvent.action)
    outbox_by_status = _grouped_count(session, OutboxEvent, OutboxEvent.status)
    source_outcomes = _grouped_count(session, SourceRecord, SourceRecord.outcome)
    reading_states = _grouped_count(session, ReadingState, ReadingState.status)

    return {
        "schema_revision": _schema_revision(session),
        "sources": {
            "count": len(sources),
            "enabled": sum(bool(row.enabled) for row in sources),
            "all_read_only": all(bool(row.read_only) for row in sources),
            "items": [
                {
                    "alias": row.alias,
                    "read_only": bool(row.read_only),
                    "enabled": bool(row.enabled),
                    "frozen_manifest_sha256": _sha256_or_none(
                        _mapping(row.config).get("_frozen_manifest_sha256")
                    ),
                }
                for row in sources
            ],
        },
        "assets": {
            "total": len(assets),
            "active": len(active_assets),
            "soft_deleted": len(assets) - len(active_assets),
            "by_type": _count_by([row.asset_type for row in active_assets]),
            "domains": dict(sorted(domains.items())),
            "domain_count": len(domains),
        },
        "source_records": {
            "total": _scalar_count(session, SourceRecord),
            "versions": _scalar_count(session, SourceRecordVersion),
            "asset_links": _scalar_count(session, SourceRecordAsset),
            "by_outcome": source_outcomes,
        },
        "representations": {
            "total": len(representations),
            "active": len(active_representations),
            "soft_deleted": len(representations) - len(active_representations),
            "by_type": _count_by(
                [row.representation_type for row in active_representations]
            ),
            "by_availability": _count_by(
                [row.availability for row in active_representations]
            ),
            "network_required": sum(
                bool(row.network_required) for row in active_representations
            ),
            "html_candidates": html_candidates,
            "missing": missing_representations,
        },
        "relations": {
            "total": len(relations),
            "active": len(active_relations),
            "soft_deleted": len(relations) - len(active_relations),
            "by_type": _count_by([row.relation_type for row in active_relations]),
            "by_status": _count_by(
                [row.confirmation_status for row in active_relations]
            ),
            "evidence_total": len(evidence_rows),
            "evidence_by_provider": _count_by(
                [row.provider or "source" for row in evidence_rows]
            ),
            "implemented_by_evidence": sum(
                row.relation_id in active_relation_ids
                and next(
                    (
                        relation.relation_type == "implemented_by"
                        for relation in active_relations
                        if relation.id == row.relation_id
                    ),
                    False,
                )
                for row in evidence_rows
            ),
            "dangling": dangling_relations,
            "inactive_endpoints": inactive_endpoint_relations,
            "logical_duplicates": duplicate_relations,
            "orphan_evidence": sum(
                row.relation_id not in {relation.id for relation in relations}
                for row in evidence_rows
            ),
        },
        "processing": {
            "jobs_total": len(jobs),
            "jobs_by_type": _count_by([row.job_type for row in jobs]),
            "jobs_by_status": _count_by([row.status for row in jobs]),
            "extract_jobs_by_status": _count_by(
                [
                    row.status
                    for row in jobs
                    if row.job_type == "extract_representation"
                ]
            ),
            "failed_jobs": failed_jobs,
            "activities_total": len(activities),
            "activities_by_status": _count_by([row.status for row in activities]),
            "extraction_outcomes": dict(sorted(extraction_outcomes.items())),
            "ocr_states": dict(sorted(ocr_states.items())),
            "failed_activities": failed_activities,
            "chunks_total": _scalar_count(session, DocumentChunk),
            "chunks_by_kind": _grouped_count(
                session, DocumentChunk, DocumentChunk.chunk_kind
            ),
            "chunks_by_parser": _grouped_count(
                session, DocumentChunk, DocumentChunk.parser_name
            ),
        },
        "indexes": {
            "states_total": len(index_states),
            "by_status": _count_by([row.status for row in index_states]),
            "versions": dict(sorted(index_versions.items())),
            "failed": failed_index_states,
        },
        "personal_workflow": {
            "reading_states": reading_states,
            "annotations": _scalar_count(session, Annotation),
            "annotation_revisions": _scalar_count(session, AnnotationRevision),
        },
        "integrations": {
            "items": [
                {
                    "integration_id": str(row.id),
                    "provider": row.provider,
                    "name": row.name,
                    "enabled": bool(row.enabled),
                    "status": row.status,
                }
                for row in integrations
            ],
            "sync_runs": _grouped_count(session, SyncRun, SyncRun.status),
            "sync_conflicts": _grouped_count(
                session, SyncConflict, SyncConflict.status
            ),
            "pending_conflicts": pending_conflicts,
            "tombstones": _grouped_count(session, Tombstone, Tombstone.status),
        },
        "backup": {
            "runs": [
                {
                    "backup_id": str(row.id),
                    "status": row.status,
                    "kind": row.backup_kind,
                    "local_copy_only": bool(row.local_copy_only),
                    "verified": row.verified_at is not None,
                    "restore_recorded": row.restored_at is not None,
                }
                for row in backup_runs
            ],
        },
        "audit": {
            "events_total": _scalar_count(session, AuditEvent),
            "by_action": audit_by_action,
            "outbox_total": _scalar_count(session, OutboxEvent),
            "outbox_by_status": outbox_by_status,
        },
    }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def _is_nonnegative_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _sha256_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return None
    return normalized


def _report_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": document.get("schema") or document.get("schema_version"),
        "status": document.get("status", "unknown"),
        "generated_at": (
            document.get("generated_at")
            or document.get("finished_at")
            or document.get("started_at")
        ),
    }


def _inventory_summary(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    def sources_by_alias(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        values = document.get("sources")
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return {}
        return {
            str(item.get("id")): item
            for item in values
            if isinstance(item, Mapping) and item.get("id")
        }

    current_sources = sources_by_alias(current)
    baseline_sources = sources_by_alias(baseline)
    common = sorted(current_sources.keys() & baseline_sources.keys())
    changed: list[dict[str, Any]] = []
    for alias in common:
        current_manifest = _mapping(current_sources[alias].get("manifest"))
        baseline_manifest = _mapping(baseline_sources[alias].get("manifest"))
        current_fingerprint = (
            current_manifest.get("sha256"),
            current_manifest.get("mtime_ns"),
        )
        baseline_fingerprint = (
            baseline_manifest.get("sha256"),
            baseline_manifest.get("mtime_ns"),
        )
        if current_fingerprint != baseline_fingerprint:
            changed.append(
                {
                    "source": alias,
                    "baseline_sha256": baseline_manifest.get("sha256"),
                    "current_sha256": current_manifest.get("sha256"),
                    "baseline_mtime_ns": baseline_manifest.get("mtime_ns"),
                    "current_mtime_ns": current_manifest.get("mtime_ns"),
                }
            )
    totals = _mapping(current.get("totals"))
    return {
        "status": current.get("status", "unavailable"),
        "source_count": _integer(totals, "source_count"),
        "source_record_count": _integer(totals, "source_record_count"),
        "all_declared_read_only": all(
            bool(item.get("read_only")) for item in current_sources.values()
        ),
        "common_sources_compared": len(common),
        "added_sources": sorted(current_sources.keys() - baseline_sources.keys()),
        "removed_sources": sorted(baseline_sources.keys() - current_sources.keys()),
        "changed_manifest_fingerprints": changed,
        "sources": [
            {
                "alias": alias,
                "records": _integer(_mapping(item.get("manifest")), "records"),
                "read_only": bool(item.get("read_only")),
                "manifest_sha256": _mapping(item.get("manifest")).get("sha256"),
                "manifest_mtime_ns": _mapping(item.get("manifest")).get("mtime_ns"),
            }
            for alias, item in sorted(current_sources.items())
        ],
    }


def _html_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    selection = _mapping(document.get("selection"))
    summary = _mapping(document.get("summary"))
    representations = document.get("representations")
    network_items: list[dict[str, Any]] = []
    if isinstance(representations, Sequence) and not isinstance(
        representations, (str, bytes)
    ):
        for item in representations:
            if not isinstance(item, Mapping):
                continue
            static = _mapping(item.get("static"))
            if static.get("inferred_policy") != "network_required":
                continue
            network_items.append(
                {
                    "representation_id": str(item.get("representation_id")),
                    "asset_id": str(item.get("asset_id")),
                    "inferred_policy": "network_required",
                }
            )
    return {
        "status": document.get("status", "unavailable"),
        "registered_candidates": _integer(selection, "registered_html_candidates"),
        "static_statuses": dict(_mapping(summary.get("static_statuses"))),
        "inferred_policies": dict(_mapping(summary.get("inferred_policies"))),
        "policy_mismatches": _integer(summary, "policy_mismatches"),
        "active_external_dependencies": _integer(
            summary, "active_external_dependencies"
        ),
        "missing_local_dependencies": _integer(
            summary, "missing_local_dependencies"
        ),
        "dynamic_requested": _integer(summary, "dynamic_requested"),
        "dynamic_completed": _integer(summary, "dynamic_completed"),
        "dynamic_probe_status": summary.get("dynamic_probe_status"),
        "network_required_items": network_items,
    }


def _source_integrity_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    counts = {
        str(key): int(value)
        for key, value in _mapping(document.get("counts")).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    raw_mismatches = document.get("mismatches")
    mismatches: list[dict[str, Any]] = []
    if isinstance(raw_mismatches, Sequence) and not isinstance(
        raw_mismatches, (str, bytes)
    ):
        for item in raw_mismatches:
            if not isinstance(item, Mapping):
                continue
            raw_reasons = item.get("reasons")
            reasons = (
                [str(value)[:127] for value in raw_reasons]
                if isinstance(raw_reasons, Sequence)
                and not isinstance(raw_reasons, (str, bytes))
                else []
            )
            mismatches.append(
                {
                    "representation_id": str(item.get("representation_id") or ""),
                    "source_alias": str(item.get("source_alias") or ""),
                    "path_ref": _safe_registered_path(
                        str(item.get("path_ref") or "")
                    ),
                    "reasons": reasons,
                }
            )
    return {
        "status": document.get("status", "unavailable"),
        "read_only": document.get("read_only") is True,
        "counts": counts,
        "mismatches": mismatches,
    }


def _index_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": document.get("status", "completed" if document else "unavailable"),
        "index_version": document.get("index_version"),
        "keyword_counts": dict(
            _mapping(
                document.get("keyword_counts_after")
                or document.get("keyword_counts")
            )
        ),
    }


def _restore_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    database = _mapping(document.get("database"))
    validation = _mapping(database.get("validation"))
    manifest = _mapping(document.get("manifest_validation"))
    rebuild = _mapping(document.get("index_rebuild"))
    cleanup = _mapping(document.get("cleanup"))
    restored = _mapping(database.get("restored"))
    return {
        "status": document.get("status", "unavailable"),
        "snapshot_id": document.get("snapshot_id"),
        "manifest_status": manifest.get("status"),
        "files_checked": _integer(manifest, "files_checked"),
        "database_status": validation.get("status"),
        "restored_counts": dict(_mapping(restored.get("counts"))),
        "index_rebuild_status": rebuild.get("status"),
        "fixed_query": {
            "keyword_exact_hit": _mapping(rebuild.get("fixed_query")).get(
                "keyword_exact_hit"
            ),
        },
        "cleanup_status": cleanup.get("status"),
        "active_resources_mutated": _mapping(document.get("safety")).get(
            "active_resources_mutated"
        ),
    }


def _evaluation_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    metrics = _mapping(document.get("metrics"))
    checks = _mapping(document.get("checks"))
    return {
        "status": document.get("status", "unavailable"),
        "metrics": {
            str(key): value
            for key, value in metrics.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        },
        "checks": {
            str(key): bool(value)
            for key, value in checks.items()
            if isinstance(value, bool)
        },
        "question_count": (
            document.get("question_count")
            or _mapping(document.get("fixture")).get("question_count")
        ),
        "provider": document.get("provider"),
        "model": document.get("model"),
    }


def _gate(
    identifier: str,
    label: str,
    status: GateStatus,
    *,
    observed: object,
    expected: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "label": label,
        "status": status,
        "observed": observed,
        "expected": expected,
        "detail": detail,
    }


def build_release_report(
    inputs: ReleaseReportInputs,
    *,
    legacy: LegacyReference | None = None,
) -> dict[str, Any]:
    """Build a deterministic gate report from a DB snapshot and report documents."""

    legacy = legacy or LegacyReference()
    sanitized_database = _privacy_safe_value(inputs.database)
    if not isinstance(sanitized_database, Mapping):
        raise TypeError("database snapshot must be a mapping")
    database = sanitized_database
    reports = inputs.reports
    assets = _mapping(database.get("assets"))
    source_records = _mapping(database.get("source_records"))
    representations = _mapping(database.get("representations"))
    relations = _mapping(database.get("relations"))
    processing = _mapping(database.get("processing"))
    indexes = _mapping(database.get("indexes"))
    integrations = _mapping(database.get("integrations"))
    audit = _mapping(database.get("audit"))
    sources = _mapping(database.get("sources"))

    inventory = _inventory_summary(
        _mapping(reports.get("inventory_current")),
        _mapping(reports.get("inventory_baseline")),
    )
    source_integrity = _source_integrity_summary(
        _mapping(reports.get("source_integrity"))
    )
    html = _html_summary(_mapping(reports.get("html_audit")))
    index_build = _index_summary(_mapping(reports.get("index_build")))
    search = _evaluation_summary(_mapping(reports.get("search_evaluation")))
    restore = _restore_summary(_mapping(reports.get("restore_rehearsal")))
    formal_import = _mapping(reports.get("formal_import"))
    import_counters = _mapping(formal_import.get("counters"))
    recovery_fixture = _mapping(reports.get("recovery_fixture"))

    by_type = _mapping(assets.get("by_type"))
    paper_count = _integer(by_type, "paper")
    repository_count = _integer(by_type, "repository")
    domain_count = _integer(assets, "domain_count")
    missing = representations.get("missing")
    missing_count = (
        len(missing)
        if isinstance(missing, Sequence) and not isinstance(missing, (str, bytes))
        else 0
    )
    implemented_by = _integer(_mapping(relations.get("by_type")), "implemented_by")
    implemented_by_evidence = _integer(relations, "implemented_by_evidence")

    gates: list[dict[str, Any]] = []
    schema_revision = database.get("schema_revision")
    gates.append(
        _gate(
            "migration-head",
            "数据库迁移版本",
            "pass" if schema_revision == "0003_search_rag_integrations" else "fail",
            observed=schema_revision,
            expected="0003_search_rag_integrations",
            detail="PostgreSQL 为正式验证目标；SQLite 仅用于测试。",
        )
    )
    source_read_only = bool(sources.get("all_read_only")) and bool(
        inventory.get("all_declared_read_only")
    )
    gates.append(
        _gate(
            "source-read-only",
            "来源只读约束",
            "pass" if source_read_only else "fail",
            observed=source_read_only,
            expected="数据库与来源清单均全部 read_only=true",
            detail="报告不包含来源根绝对路径。",
        )
    )
    changed_fingerprints = inventory.get("changed_manifest_fingerprints")
    removed_sources = inventory.get("removed_sources")
    common_sources = int(inventory.get("common_sources_compared") or 0)
    inventory_source_items = inventory.get("sources")
    inventory_manifest_by_alias = {
        str(item.get("alias")): _sha256_or_none(item.get("manifest_sha256"))
        for item in (
            inventory_source_items
            if isinstance(inventory_source_items, Sequence)
            and not isinstance(inventory_source_items, (str, bytes))
            else []
        )
        if isinstance(item, Mapping) and item.get("alias")
    }
    database_source_items = sources.get("items")
    frozen_manifest_by_alias = {
        str(item.get("alias")): _sha256_or_none(
            item.get("frozen_manifest_sha256")
        )
        for item in (
            database_source_items
            if isinstance(database_source_items, Sequence)
            and not isinstance(database_source_items, (str, bytes))
            else []
        )
        if isinstance(item, Mapping) and item.get("alias")
    }
    inventory_aliases = set(inventory_manifest_by_alias)
    database_aliases = set(frozen_manifest_by_alias)
    missing_database_sources = sorted(inventory_aliases - database_aliases)
    missing_inventory_sources = sorted(database_aliases - inventory_aliases)
    missing_frozen_fingerprints = sorted(
        alias
        for alias in inventory_aliases & database_aliases
        if frozen_manifest_by_alias[alias] is None
    )
    missing_inventory_fingerprints = sorted(
        alias
        for alias, digest in inventory_manifest_by_alias.items()
        if digest is None
    )
    frozen_fingerprint_mismatches = [
        {
            "source": alias,
            "inventory_sha256": inventory_manifest_by_alias[alias],
            "frozen_sha256": frozen_manifest_by_alias[alias],
        }
        for alias in sorted(inventory_aliases & database_aliases)
        if inventory_manifest_by_alias[alias] is not None
        and frozen_manifest_by_alias[alias] is not None
        and inventory_manifest_by_alias[alias] != frozen_manifest_by_alias[alias]
    ]
    frozen_fingerprints_ok = not any(
        (
            missing_database_sources,
            missing_inventory_sources,
            missing_frozen_fingerprints,
            missing_inventory_fingerprints,
            frozen_fingerprint_mismatches,
        )
    )
    fingerprints_ok = (
        common_sources >= legacy.domain_count
        and not changed_fingerprints
        and not removed_sources
        and frozen_fingerprints_ok
    )
    gates.append(
        _gate(
            "source-fingerprints",
            "既有来源 manifest 未变",
            "pass" if fingerprints_ok else "fail",
            observed={
                "changed": changed_fingerprints,
                "removed": removed_sources,
                "added": inventory.get("added_sources"),
                "common_sources_compared": common_sources,
                "missing_database_sources": missing_database_sources,
                "missing_inventory_sources": missing_inventory_sources,
                "missing_frozen_fingerprints": missing_frozen_fingerprints,
                "missing_inventory_fingerprints": missing_inventory_fingerprints,
                "frozen_fingerprint_mismatches": frozen_fingerprint_mismatches,
            },
            expected=(
                "共同来源 hash/mtime 不变；每个当前来源 inventory SHA-256 "
                "精确匹配数据库冻结值"
            ),
            detail=(
                "新增来源允许扩展，但首次正式导入后也必须具有冻结 digest；"
                "缺来源、缺 digest 或不一致均阻止发布。"
            ),
        )
    )
    integrity_counts = _mapping(source_integrity.get("counts"))
    representations_seen = _integer(integrity_counts, "representations_seen")
    integrity_accounted = sum(
        _integer(integrity_counts, key)
        for key in (
            "files_verified",
            "directories_verified",
            "missing_confirmed",
            "external_skipped",
        )
    )
    source_integrity_ok = (
        source_integrity.get("status") == "passed"
        and source_integrity.get("read_only") is True
        and _integer(integrity_counts, "mismatches") == 0
        and representations_seen == _integer(representations, "active")
        and integrity_accounted == representations_seen
    )
    gates.append(
        _gate(
            "source-representation-integrity",
            "来源文件与登记指纹一致",
            "pass" if source_integrity_ok else "fail",
            observed={
                "status": source_integrity.get("status"),
                "read_only": source_integrity.get("read_only"),
                "representations_seen": representations_seen,
                "representations_accounted": integrity_accounted,
                "mismatches": _integer(integrity_counts, "mismatches"),
            },
            expected="全部 active 表示有结局；文件 SHA-256/size/mtime 与目录 tree digest 一致",
            detail="验证报告仅包含相对登记路径和原因码，不含来源根或正文。",
        )
    )
    assets_ok = (
        paper_count >= legacy.paper_assets
        and repository_count >= legacy.repository_assets
        and domain_count >= legacy.domain_count
    )
    gates.append(
        _gate(
            "legacy-asset-floor",
            "六领域资产基线仍被覆盖",
            "pass" if assets_ok else "fail",
            observed={
                "papers": paper_count,
                "repositories": repository_count,
                "domains": domain_count,
            },
            expected=(
                f"papers≥{legacy.paper_assets}, repositories≥"
                f"{legacy.repository_assets}, domains≥{legacy.domain_count}"
            ),
            detail="数量是兼容下限，不是第七领域加入后的精确上限。",
        )
    )
    source_record_total = _integer(source_records, "total")
    inventory_records = int(inventory.get("source_record_count") or 0)
    records_ok = (
        source_record_total >= legacy.source_records
        and inventory_records == source_record_total
    )
    gates.append(
        _gate(
            "source-record-accounting",
            "来源记录零静默丢失",
            "pass" if records_ok else "fail",
            observed={
                "database": source_record_total,
                "inventory": inventory_records,
                "legacy_floor": legacy.source_records,
            },
            expected="数据库记录数等于当前来源清单，且不低于六领域基线",
            detail="新增领域记录包含在当前口径中。",
        )
    )
    missing_ok = (
        missing_count == _integer(
            _mapping(representations.get("by_availability")), "missing"
        )
        and missing_count >= legacy.known_missing_representations
    )
    gates.append(
        _gate(
            "missing-representations-visible",
            "缺失表示显式可见",
            "pass" if missing_ok else "fail",
            observed=missing_count,
            expected=f"清单计数一致且≥已知 {legacy.known_missing_representations} 项",
            detail="每项只列稳定 ID、类型和净化后的登记相对路径。",
        )
    )
    graph_ok = (
        _integer(relations, "dangling") == 0
        and _integer(relations, "logical_duplicates") == 0
        and _integer(relations, "orphan_evidence") == 0
        and implemented_by >= legacy.implemented_by_relations
        and implemented_by_evidence >= legacy.implemented_by_evidence
    )
    gates.append(
        _gate(
            "graph-integrity",
            "关系图谱完整性",
            "pass" if graph_ok else "fail",
            observed={
                "dangling": _integer(relations, "dangling"),
                "logical_duplicates": _integer(relations, "logical_duplicates"),
                "orphan_evidence": _integer(relations, "orphan_evidence"),
                "implemented_by": implemented_by,
                "implemented_by_evidence": implemented_by_evidence,
            },
            expected=(
                "悬空/重复/孤儿证据=0，implemented_by≥"
                f"{legacy.implemented_by_relations}，证据≥"
                f"{legacy.implemented_by_evidence}"
            ),
            detail="多来源依据计入 evidence，不复制逻辑边。",
        )
    )
    relation_candidates = _integer(import_counters, "relation_candidates")
    resolved_candidates = _integer(import_counters, "relation_candidates_resolved")
    unresolved_candidates = _integer(import_counters, "relation_candidates_unresolved")
    representation_candidates = _integer(import_counters, "representation_candidates")
    represented_candidates = _integer(
        import_counters, "representations_available"
    ) + _integer(import_counters, "representations_missing")
    candidate_accounting = (
        relation_candidates >= legacy.relation_candidates
        and resolved_candidates + unresolved_candidates == relation_candidates
        and representation_candidates >= legacy.representation_candidates
        and represented_candidates == representation_candidates
    )
    gates.append(
        _gate(
            "candidate-accounting",
            "表示与关系候选均有结局",
            "pass" if candidate_accounting else "fail",
            observed={
                "relation_candidates": relation_candidates,
                "relation_accounted": resolved_candidates + unresolved_candidates,
                "representation_candidates": representation_candidates,
                "representation_accounted": represented_candidates,
            },
            expected="候选数=已结局数，且覆盖六领域基线",
            detail="未解析候选被显式保存，不视为静默丢失。",
        )
    )
    formal_schema_version = formal_import.get("schema_version")
    formal_schema_valid = (
        isinstance(formal_schema_version, int)
        and not isinstance(formal_schema_version, bool)
        and formal_schema_version == _FORMAL_IMPORT_SCHEMA_VERSION
    )
    validated_dry_run = _mapping(formal_import.get("validated_dry_run"))
    validated_dry_run_status = validated_dry_run.get("status")
    validated_dry_run_unapproved = validated_dry_run.get(
        "unapproved_review_groups"
    )
    validated_dry_run_valid = (
        bool(validated_dry_run)
        and validated_dry_run_status == "passed"
        and _is_nonnegative_integer(validated_dry_run_unapproved)
        and validated_dry_run_unapproved == 0
    )
    missing_required_import_counters = sorted(
        _FORMAL_IMPORT_REQUIRED_COUNTERS - set(import_counters)
    )
    invalid_required_import_counters = sorted(
        key
        for key in _FORMAL_IMPORT_REQUIRED_COUNTERS & set(import_counters)
        if not _is_nonnegative_integer(import_counters[key])
    )
    idempotency_created_counts = {
        key: _integer(import_counters, key)
        for key in _FORMAL_IMPORT_IDEMPOTENCY_COUNTERS
    }
    import_created = sum(idempotency_created_counts.values())
    import_status = formal_import.get("status")
    import_source_modified = formal_import.get("source_roots_modified")
    unapproved_merge_groups = _integer(
        import_counters,
        "unapproved_merge_groups",
    )
    if (
        not formal_schema_valid
        or not validated_dry_run_valid
        or missing_required_import_counters
        or invalid_required_import_counters
        or import_status != "passed"
        or import_source_modified is not False
        or unapproved_merge_groups
        or import_created
    ):
        import_gate: GateStatus = "fail"
    else:
        import_gate = "pass"
    gates.append(
        _gate(
            "idempotent-import",
            "第二次正式导入幂等",
            import_gate,
            observed={
                "schema_version": formal_schema_version,
                "schema_valid": formal_schema_valid,
                "validated_dry_run": {
                    "present": bool(validated_dry_run),
                    "status": validated_dry_run_status,
                    "unapproved_review_groups": validated_dry_run_unapproved,
                    "valid": validated_dry_run_valid,
                },
                "missing_required_counters": missing_required_import_counters,
                "invalid_required_counters": invalid_required_import_counters,
                "status": import_status,
                "new_entities": import_created,
                "created_counters": idempotency_created_counts,
                "source_roots_modified": import_source_modified,
                "unapproved_merge_groups": unapproved_merge_groups,
            },
            expected=(
                f"formal schema v{_FORMAL_IMPORT_SCHEMA_VERSION}、必需计数完整且有效、"
                "全来源 dry-run 已通过且未批准组=0、全部幂等新增计数=0、"
                "来源未修改、未批准弱合并=0"
            ),
            detail=(
                "发布报告只接受第二次正式导入产物；首次导入的非零新增不能作为"
                "可发布的幂等证据。"
            ),
        )
    )
    extract_statuses = _mapping(processing.get("extract_jobs_by_status"))
    unfinished_extractions = _integer(extract_statuses, "pending") + _integer(
        extract_statuses, "running"
    )
    failed_extractions = _integer(extract_statuses, "failed")
    extraction_gate: GateStatus = (
        "fail" if unfinished_extractions else ("warn" if failed_extractions else "pass")
    )
    gates.append(
        _gate(
            "extraction-terminal",
            "抽取/OCR 作业已收敛",
            extraction_gate,
            observed={
                "unfinished": unfinished_extractions,
                "failed": failed_extractions,
                "ocr_states": processing.get("ocr_states"),
                "chunks": processing.get("chunks_total"),
            },
            expected="pending/running=0；失败均进入显式失败清单",
            detail="已知不可抽取文件可以作为警告发布，但不得隐藏。",
        )
    )
    index_ready = bool(index_build) and bool(index_build.get("keyword_counts"))
    if not index_ready and not index_build:
        index_status: GateStatus = "fail"
    elif index_ready:
        index_status = "pass"
    else:
        index_status = "fail"
    gates.append(
        _gate(
            "production-index",
            "生产关键词索引",
            index_status,
            observed=index_build,
            expected="Meilisearch 关键词索引存在且计数非空",
            detail="Meilisearch 是可重建状态，但发布时必须有验证产物。",
        )
    )
    gates.append(
        _gate(
            "search-evaluation",
            "搜索质量与延迟评测",
            "pass" if search.get("status") == "passed" else "fail",
            observed=search,
            expected="固定查询评测 status=passed",
            detail="精确标识、Recall、nDCG 和热缓存 P95 由独立报告给出。",
        )
    )
    html_db_count = _integer(representations, "html_candidates")
    html_coverage = int(html.get("registered_candidates") or 0) >= html_db_count
    html_status: GateStatus = (
        "fail"
        if not html_coverage
        else (
            "warn"
            if html.get("dynamic_probe_status") != "completed"
            or int(html.get("active_external_dependencies") or 0) > 0
            or int(html.get("missing_local_dependencies") or 0) > 0
            else "pass"
        )
    )
    gates.append(
        _gate(
            "html-audit",
            "HTML 隔离与联网依赖审计",
            html_status,
            observed={
                "database_html": html_db_count,
                "audited": html.get("registered_candidates"),
                "external_dependencies": html.get("active_external_dependencies"),
                "missing_local": html.get("missing_local_dependencies"),
                "dynamic_probe": html.get("dynamic_probe_status"),
            },
            expected="静态覆盖全部登记 HTML；联网/缺失依赖显式；浏览器抽样完成",
            detail="联网依赖是可见风险，不会被误当成本地完整页面。",
        )
    )
    pending_conflicts = integrations.get("pending_conflicts")
    conflict_count = (
        len(pending_conflicts)
        if isinstance(pending_conflicts, Sequence)
        and not isinstance(pending_conflicts, (str, bytes))
        else 0
    )
    gates.append(
        _gate(
            "sync-conflicts",
            "外部同步冲突",
            "warn" if conflict_count else "pass",
            observed=conflict_count,
            expected="冲突显式入队；0 为完全收敛",
            detail="待人工决议不会造成静默覆盖。",
        )
    )
    gates.append(
        _gate(
            "recovery-fixture",
            "非空恢复验收夹具",
            "pass" if recovery_fixture.get("status") == "verified" else "fail",
            observed=_report_summary(recovery_fixture),
            expected="status=verified，覆盖批注修订、阅读、关系、冲突、tombstone、审计/outbox",
            detail="夹具是软删除的合成系统数据，不含用户内容。",
        )
    )
    restore_ok = (
        restore.get("status") == "verified"
        and restore.get("manifest_status") == "verified"
        and restore.get("database_status") == "verified"
        and restore.get("index_rebuild_status") == "verified"
        and restore.get("cleanup_status") == "verified"
        and restore.get("active_resources_mutated") is False
    )
    gates.append(
        _gate(
            "isolated-restore",
            "空环境隔离恢复演练",
            "pass" if restore_ok else "fail",
            observed=restore,
            expected="文件、数据库、索引与固定查询验证通过，活跃环境未修改",
            detail="索引卷不备份，恢复后从正式数据重建。",
        )
    )
    outbox_statuses = _mapping(audit.get("outbox_by_status"))
    failed_outbox = _integer(outbox_statuses, "failed")
    pending_outbox = _integer(outbox_statuses, "pending") + _integer(
        outbox_statuses, "processing"
    )
    audit_count = _integer(audit, "events_total")
    audit_status: GateStatus = (
        "fail"
        if audit_count == 0 or failed_outbox
        else ("warn" if pending_outbox else "pass")
    )
    gates.append(
        _gate(
            "audit-outbox",
            "审计与事务 Outbox",
            audit_status,
            observed={
                "audit_events": audit_count,
                "outbox_statuses": dict(outbox_statuses),
            },
            expected="审计非空、failed outbox=0；未发布事件显式可见",
            detail="失败报告不包含 payload、异常正文或请求密钥。",
        )
    )
    backup_runs = _mapping(database.get("backup")).get("runs")
    local_only = restore_ok or any(
        isinstance(item, Mapping) and bool(item.get("local_copy_only"))
        for item in (
            backup_runs
            if isinstance(backup_runs, Sequence)
            and not isinstance(backup_runs, (str, bytes))
            else []
        )
    )
    gates.append(
        _gate(
            "backup-risk",
            "备份介质风险",
            "warn" if local_only or restore_ok else "fail",
            observed={
                "database_runs": backup_runs,
                "restore_verified": restore_ok,
                "local_copy_only": local_only,
            },
            expected="至少一次可恢复快照；同机副本持续标记风险",
            detail="local-copy-only 不阻止本地 v1，但不能替代异机备份。",
        )
    )

    gate_counts = Counter(str(item["status"]) for item in gates)
    overall_status: GateStatus = (
        "fail"
        if gate_counts["fail"]
        else ("warn" if gate_counts["warn"] else "pass")
    )
    failed_jobs = processing.get("failed_jobs")
    failed_activities = processing.get("failed_activities")
    failed_index_states = indexes.get("failed")
    report_summaries = {
        name: _report_summary(document)
        for name, document in sorted(reports.items())
    }
    generated_at = (inputs.generated_at or _now()).astimezone(UTC)
    return {
        "schema": "research-asset-hub.release-report/v1",
        "generated_at": generated_at.isoformat(),
        "status": overall_status,
        "gate_summary": {
            "pass": gate_counts["pass"],
            "warn": gate_counts["warn"],
            "fail": gate_counts["fail"],
        },
        "scope": {
            "legacy_six_domain_reference": asdict(legacy),
            "current": {
                "domains": domain_count,
                "papers": paper_count,
                "repositories": repository_count,
                "source_records": source_record_total,
            },
            "growth": {
                "domain_delta": domain_count - legacy.domain_count,
                "paper_delta": paper_count - legacy.paper_assets,
                "repository_delta": repository_count - legacy.repository_assets,
                "source_record_delta": source_record_total - legacy.source_records,
                "added_sources": inventory.get("added_sources"),
            },
            "interpretation": (
                "Legacy values are compatibility floors. New domains and assets are "
                "reported as growth, never as exact-count failures."
            ),
        },
        "database": dict(database),
        "source_integrity": inventory,
        "source_representation_integrity": source_integrity,
        "formal_import": {
            "schema_version": formal_schema_version,
            "schema_valid": formal_schema_valid,
            "validated_dry_run": {
                "present": bool(validated_dry_run),
                "status": validated_dry_run_status,
                "unapproved_review_groups": validated_dry_run_unapproved,
                "valid": validated_dry_run_valid,
            },
            "status": formal_import.get("status", "unavailable"),
            "source_roots_modified": formal_import.get("source_roots_modified"),
            "missing_required_counters": missing_required_import_counters,
            "invalid_required_counters": invalid_required_import_counters,
            "created_counters": idempotency_created_counts,
            "counters": {
                str(key): value
                for key, value in import_counters.items()
                if isinstance(value, (int, float, str, bool)) or value is None
            },
        },
        "html": html,
        "index_build": index_build,
        "search_evaluation": search,
        "recovery_fixture": _report_summary(recovery_fixture),
        "restore_rehearsal": restore,
        "failures": {
            "source_representation_mismatches": source_integrity.get(
                "mismatches", []
            ),
            "missing_representations": missing if isinstance(missing, Sequence) else [],
            "failed_jobs": failed_jobs if isinstance(failed_jobs, Sequence) else [],
            "failed_activities": (
                failed_activities if isinstance(failed_activities, Sequence) else []
            ),
            "failed_index_states": (
                failed_index_states
                if isinstance(failed_index_states, Sequence)
                else []
            ),
            "pending_sync_conflicts": (
                pending_conflicts if isinstance(pending_conflicts, Sequence) else []
            ),
            "network_required_html": html.get("network_required_items", []),
            "failed_gates": [
                item for item in gates if item["status"] == "fail"
            ],
            "warnings": [item for item in gates if item["status"] == "warn"],
        },
        "gates": gates,
        "evidence_reports": report_summaries,
        "privacy": {
            "contains_credentials": False,
            "contains_database_url": False,
            "contains_private_document_text": False,
            "contains_source_root_paths": False,
            "registered_paths": "relative-or-redacted",
            "error_messages": "codes-only",
        },
    }


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_release_markdown(report: Mapping[str, Any]) -> str:
    """Render the release report as a concise, human-readable Chinese report."""

    gate_summary = _mapping(report.get("gate_summary"))
    scope = _mapping(report.get("scope"))
    current = _mapping(scope.get("current"))
    growth = _mapping(scope.get("growth"))
    gates = report.get("gates")
    gate_rows = [
        (
            str(item.get("status", "")).upper(),
            item.get("label", ""),
            item.get("observed", ""),
            item.get("expected", ""),
        )
        for item in (
            gates
            if isinstance(gates, Sequence) and not isinstance(gates, (str, bytes))
            else []
        )
        if isinstance(item, Mapping)
    ]
    failures = _mapping(report.get("failures"))
    missing = failures.get("missing_representations")
    jobs = failures.get("failed_jobs")
    conflicts = failures.get("pending_sync_conflicts")
    html = _mapping(report.get("html"))
    restore = _mapping(report.get("restore_rehearsal"))
    lines = [
        "# Research Asset Hub v1 发布报告",
        "",
        f"- 总状态：**{str(report.get('status', 'unknown')).upper()}**",
        f"- 生成时间：{report.get('generated_at')}",
        (
            f"- 门禁：{gate_summary.get('pass', 0)} pass / "
            f"{gate_summary.get('warn', 0)} warn / "
            f"{gate_summary.get('fail', 0)} fail"
        ),
        "",
        "## 当前资产范围",
        "",
        _markdown_table(
            ("项目", "当前", "相对六领域基线增长"),
            (
                ("领域", current.get("domains", 0), growth.get("domain_delta", 0)),
                ("论文", current.get("papers", 0), growth.get("paper_delta", 0)),
                (
                    "仓库",
                    current.get("repositories", 0),
                    growth.get("repository_delta", 0),
                ),
                (
                    "来源记录",
                    current.get("source_records", 0),
                    growth.get("source_record_delta", 0),
                ),
            ),
        ),
        "",
        "> 六领域数字只作为兼容下限；新增第七领域与后续资产属于正常增长。",
        "",
        "## 发布门禁",
        "",
        _markdown_table(("状态", "检查", "观测", "期望"), gate_rows),
        "",
        "## 失败与人工处理清单",
        "",
        (
            f"- 缺失表示：{len(missing) if isinstance(missing, Sequence) else 0}"
        ),
        f"- 失败作业：{len(jobs) if isinstance(jobs, Sequence) else 0}",
        (
            f"- 待处理同步冲突："
            f"{len(conflicts) if isinstance(conflicts, Sequence) else 0}"
        ),
        (
            f"- 需要联网的 HTML："
            f"{len(html.get('network_required_items', []))}"
        ),
        (
            f"- HTML 本地缺失依赖："
            f"{html.get('missing_local_dependencies', 0)}"
        ),
        "",
        "## 恢复与备份",
        "",
        f"- 恢复演练：{restore.get('status', 'unavailable')}",
        f"- 文件 manifest：{restore.get('manifest_status', 'unavailable')}",
        f"- PostgreSQL：{restore.get('database_status', 'unavailable')}",
        f"- 索引重建：{restore.get('index_rebuild_status', 'unavailable')}",
        f"- 隔离环境清理：{restore.get('cleanup_status', 'unavailable')}",
        "",
        "## 隐私边界",
        "",
        "本报告不包含密钥、数据库连接串、论文/批注正文、来源根绝对路径或完整异常消息。"
        "缺失项仅保留稳定 ID 与净化后的登记相对路径。",
        "",
    ]
    return "\n".join(lines)


def write_release_report(
    report: Mapping[str, Any],
    *,
    output_directory: Path,
    validation_path: Path | None = None,
) -> dict[str, str]:
    """Atomically write JSON + Markdown and an optional validation copy."""

    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "release-report.json"
    markdown_path = output_directory / "release-report.md"
    rendered_json = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    rendered_markdown = render_release_markdown(report)
    _atomic_write_text(json_path, rendered_json)
    _atomic_write_text(markdown_path, rendered_markdown)
    if validation_path is not None:
        _atomic_write_text(validation_path, rendered_json)
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
        **({"validation": str(validation_path)} if validation_path else {}),
        "json_sha256": hashlib.sha256(rendered_json.encode("utf-8")).hexdigest(),
        "markdown_sha256": hashlib.sha256(
            rendered_markdown.encode("utf-8")
        ).hexdigest(),
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def load_json_report(path: Path) -> Mapping[str, Any]:
    """Load a JSON object without accepting array/scalar report documents."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError(f"report is not a JSON object: {path.name}")
    return document


__all__ = [
    "LegacyReference",
    "ReleaseReportInputs",
    "build_release_report",
    "collect_database_snapshot",
    "load_json_report",
    "render_release_markdown",
    "write_release_report",
]
