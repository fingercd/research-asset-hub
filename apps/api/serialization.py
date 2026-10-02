from __future__ import annotations

from datetime import datetime
from typing import Any

from apps.api.domain_views import presentation_domains
from packages.domain.models import (
    Annotation,
    AnnotationRevision,
    Asset,
    Job,
    ReadingState,
    Representation,
    SavedView,
    SyncConflict,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_asset(
    asset: Asset,
    *,
    reading_state: ReadingState | None = None,
    annotation_summary: str | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(asset.id),
        "asset_type": asset.asset_type,
        "type": asset.asset_type,
        "title": asset.title,
        "title_zh": asset.title_zh,
        "subtitle": asset.subtitle,
        "authors": list(asset.authors or []),
        "year": asset.year,
        "venue": asset.venue,
        "language": asset.language,
        "abstract": asset.abstract,
        "summary": asset.summary,
        "content": asset.content,
        "analysis": dict(asset.analysis or {}),
        "visibility": asset.visibility,
        "source_urls": list(asset.source_urls or []),
        "domain_slugs": presentation_domains(asset),
        "tags": list(asset.tags or []),
        "attributes": dict(asset.attributes or {}),
        "reading_status": reading_state.status if reading_state else "unread",
        "reading_state": (
            {"status": reading_state.status, "updated_at": _iso(reading_state.updated_at)}
            if reading_state
            else {"status": "unread"}
        ),
        "annotation_summary": annotation_summary,
        "created_at": _iso(asset.created_at),
        "updated_at": _iso(asset.updated_at),
        "deleted_at": _iso(asset.deleted_at),
        "row_version": asset.row_version,
    }
    if score is not None:
        result["score"] = score
    return result


def serialize_representation(representation: Representation) -> dict[str, Any]:
    return {
        "id": str(representation.id),
        "asset_id": str(representation.asset_id),
        "source_id": (
            str(representation.source_id) if representation.source_id is not None else None
        ),
        "representation_type": representation.representation_type,
        "type": representation.representation_type,
        "role": representation.role,
        "materialization_kind": representation.materialization_kind,
        "availability": representation.availability,
        "root_key": representation.root_key,
        "path_ref": representation.path_ref,
        "external_url": representation.external_url,
        "entrypoint": representation.entrypoint,
        "revision": representation.revision,
        "sha256": representation.sha256,
        "mime_type": representation.mime_type,
        "size_bytes": representation.size_bytes,
        "network_required": representation.network_required,
        "attributes": dict(representation.attributes or {}),
        "created_at": _iso(representation.created_at),
        "updated_at": _iso(representation.updated_at),
        "row_version": representation.row_version,
    }


def serialize_quick_action(
    *,
    kind: str,
    status: str,
    representation_id: str | None,
    owner_asset_id: str | None,
    relation_type: str | None,
    reason: str | None,
    alternatives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": status,
        "representation_id": representation_id,
        "owner_asset_id": owner_asset_id,
        "relation_type": relation_type,
        "reason": reason,
        "alternatives": list(alternatives or []),
    }


def serialize_research_unit(
    *,
    unit_id: str,
    unit_kind: str,
    primary_asset: dict[str, Any],
    related_repositories: list[dict[str, Any]],
    quick_actions: list[dict[str, Any]],
    matched_assets: list[dict[str, Any]],
    score: float,
    snippet: str | None,
    match_source: str,
) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "unit_kind": unit_kind,
        "primary_asset": primary_asset,
        "related_repositories": related_repositories,
        "quick_actions": quick_actions,
        "matched_assets": matched_assets,
        "covered_asset_count": 1 + len(related_repositories),
        "score": score,
        "snippet": snippet,
        "match_source": match_source,
    }


def serialize_domain_overview(
    *,
    asset_id: str | None,
    representation_id: str | None,
    status: str,
    default_presentation: str,
    last_verified_at: datetime | None,
    warning: str | None,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "representation_id": representation_id,
        "status": status,
        "default_presentation": default_presentation,
        "last_verified_at": _iso(last_verified_at),
        "warning": warning,
    }


def serialize_annotation(annotation: Annotation) -> dict[str, Any]:
    return {
        "id": str(annotation.id),
        "asset_id": str(annotation.asset_id),
        "representation_id": (
            str(annotation.representation_id) if annotation.representation_id is not None else None
        ),
        "kind": annotation.kind,
        "title": annotation.title,
        "summary": annotation.summary,
        "body_markdown": annotation.body_markdown,
        "body": annotation.body_markdown,
        "anchor": annotation.anchor,
        "anchor_kind": annotation.anchor_kind,
        "anchor_status": annotation.anchor_status,
        "pinned": annotation.pinned,
        "created_at": _iso(annotation.created_at),
        "updated_at": _iso(annotation.updated_at),
        "deleted_at": _iso(annotation.deleted_at),
        "row_version": annotation.row_version,
    }


def serialize_annotation_revision(revision: AnnotationRevision) -> dict[str, Any]:
    return {
        "id": str(revision.id),
        "annotation_id": str(revision.annotation_id),
        "revision": revision.revision,
        "title": revision.title,
        "summary": revision.summary,
        "body_markdown": revision.body_markdown,
        "anchor": revision.anchor,
        "editor": revision.editor,
        "source": revision.source,
        "created_at": _iso(revision.created_at),
    }


def serialize_job(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "job_type": job.job_type,
        "type": job.job_type,
        "name": job.job_type,
        "parameters": dict(job.parameters or {}),
        "status": job.status,
        "priority": job.priority,
        "progress": job.progress,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "scheduled_at": _iso(job.scheduled_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "heartbeat_at": _iso(job.heartbeat_at),
        "cancel_requested": job.cancel_requested,
        "error": job.error,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "row_version": job.row_version,
    }


def serialize_comparison(view: SavedView, assets: list[dict[str, Any]]) -> dict[str, Any]:
    definition = dict(view.definition or {})
    return {
        "id": str(view.id),
        "title": view.name,
        "asset_ids": list(definition.get("asset_ids", [])),
        "assets": assets,
        "columns": definition.get("columns", []),
        "synthesis": definition.get("synthesis"),
        "created_at": _iso(view.created_at),
        "updated_at": _iso(view.updated_at),
    }


def serialize_sync_conflict(item: SyncConflict) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "entity_type": item.entity_type,
        "entity_id": str(item.entity_id),
        "asset_id": str(item.entity_id) if item.entity_type == "asset" else None,
        "field": item.field_path,
        "field_path": item.field_path,
        "status": item.status,
        "base_value": item.base_value,
        "local_value": item.hub_value,
        "hub_value": item.hub_value,
        "incoming_value": item.external_value,
        "external_value": item.external_value,
        "resolution": item.resolution,
        "resolved_value": item.resolved_value,
        "resolved_at": _iso(item.resolved_at),
        "detected_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "row_version": item.row_version,
    }
