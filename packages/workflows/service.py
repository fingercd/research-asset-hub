"""Audited workflow mutations, independent from immutable source metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.events import record_mutation
from apps.api.problem import ApiProblem, not_found
from apps.api.routes_v1 import _ensure_override_base_claim, _live_asset, _serialize_override
from apps.api.serialization import serialize_asset
from packages.domain.enums import AuditAction
from packages.domain.models import Asset, FieldResolution, ReadingState, Relation
from packages.workflows.contracts import BulkUpdate


def audit(
    session: Session,
    entity: Any,
    entity_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    request_id: str | None,
    *,
    action: AuditAction | None = None,
) -> None:
    action = action or (AuditAction.CREATE if before is None else AuditAction.UPDATE)
    record_mutation(
        session,
        entity_type=entity_type,
        entity_id=entity.id,
        action=action,
        before=before,
        after=after,
        request_id=request_id,
        event_type=f"{entity_type}.{action.value}",
    )


def live_collection(session: Session, collection_id: UUID) -> Asset:
    item = _live_asset(session, collection_id)
    if item.asset_type != "collection":
        raise not_found("Collection", collection_id)
    return item


def set_membership(
    session: Session, asset: Asset, collection: Asset, add: bool, request_id: str | None
) -> bool:
    session.refresh(collection, with_for_update=True)
    if collection.deleted_at is not None:
        raise not_found("Collection", collection.id)
    if asset.asset_type == "collection":
        raise ApiProblem(422, "集合成员无效", "Collections cannot be collection members.")
    relation = session.scalar(
        select(Relation)
        .where(
            Relation.subject_asset_id == asset.id,
            Relation.object_asset_id == collection.id,
            Relation.relation_type == "belongs_to_collection",
            Relation.source_key == "manual",
        )
        .with_for_update()
    )
    if (add and relation is not None and relation.deleted_at is None) or (
        not add and (relation is None or relation.deleted_at is not None)
    ):
        return False
    before = None if relation is None else membership_snapshot(relation)
    if relation is None:
        relation = Relation(
            subject_asset_id=asset.id,
            object_asset_id=collection.id,
            relation_type="belongs_to_collection",
            source_key="manual",
            confirmation_status="confirmed",
            evidence={"origin": "user-workflow"},
        )
        session.add(relation)
    else:
        relation.deleted_at = None if add else datetime.now(UTC)
    session.flush()
    audit(
        session,
        relation,
        "relation",
        before,
        membership_snapshot(relation),
        request_id,
        action=AuditAction.CREATE
        if before is None
        else (AuditAction.RESTORE if add else AuditAction.DELETE),
    )
    return True


def membership_snapshot(relation: Relation) -> dict[str, Any]:
    return {
        "id": str(relation.id),
        "subject_asset_id": str(relation.subject_asset_id),
        "object_asset_id": str(relation.object_asset_id),
        "relation_type": relation.relation_type,
        "deleted_at": relation.deleted_at.isoformat() if relation.deleted_at else None,
        "row_version": relation.row_version,
    }


def override_list(
    session: Session, asset: Asset, field: str, value: list[str], request_id: str | None
) -> bool:
    if getattr(asset, field) == value:
        return False
    asset_before = serialize_asset(asset)
    resolution = session.scalar(
        select(FieldResolution)
        .where(
            FieldResolution.entity_type == "asset",
            FieldResolution.entity_id == asset.id,
            FieldResolution.field_path == f"/{field}",
        )
        .with_for_update()
    )
    before = _serialize_override(resolution) if resolution else None
    if resolution is None:
        resolution = FieldResolution(
            entity_type="asset",
            entity_id=asset.id,
            field_path=f"/{field}",
            resolution_kind="override",
            override_value=value,
            reason="User workflow bulk edit",
            resolved_by="local",
        )
        session.add(resolution)
        session.flush()
    _ensure_override_base_claim(session, resolution, asset)
    resolution.resolution_kind = "override"
    resolution.selected_claim_id = None
    resolution.override_value = value
    resolution.reason = "User workflow bulk edit"
    resolution.resolved_by = "local"
    setattr(asset, field, value)
    session.flush()
    audit(
        session, resolution, "field_override", before, _serialize_override(resolution), request_id
    )
    audit(session, asset, "asset", asset_before, serialize_asset(asset), request_id)
    return True


def update_reading_status(
    session: Session, asset: Asset, status: str, request_id: str | None
) -> bool:
    state = session.scalar(
        select(ReadingState)
        .where(
            ReadingState.asset_id == asset.id,
            ReadingState.user_id == "local",
        )
        .with_for_update()
    )
    if state is not None and state.status == status:
        return False
    if state is None and status == "unread":
        return False
    before = (
        {"asset_id": str(asset.id), "status": state.status, "row_version": state.row_version}
        if state
        else None
    )
    if state is None:
        state = ReadingState(asset_id=asset.id, user_id="local")
        session.add(state)
    now = datetime.now(UTC)
    state.status = status
    if status in {"reading", "read"}:
        state.started_at = state.started_at or now
    if status == "read":
        state.read_at = now
    elif status == "unread":
        state.started_at = None
        state.read_at = None
    else:
        state.read_at = None
    session.flush()
    audit(
        session,
        state,
        "reading_state",
        before,
        {"asset_id": str(asset.id), "status": state.status, "row_version": state.row_version},
        request_id,
    )
    return True


def apply_bulk_item(
    session: Session, asset_id: UUID, payload: BulkUpdate, request_id: str | None
) -> str:
    asset = _live_asset(session, asset_id)
    session.refresh(asset, with_for_update=True)
    changed = False
    for field, add, remove in (
        ("tags", payload.add_tags, payload.remove_tags),
        ("domain_slugs", payload.add_domains, payload.remove_domains),
    ):
        if add or remove:
            new = list(
                dict.fromkeys([name for name in getattr(asset, field) if name not in remove] + add)
            )
            changed = override_list(session, asset, field, new, request_id) or changed
    if payload.reading_status is not None:
        changed = (
            update_reading_status(session, asset, payload.reading_status, request_id) or changed
        )
    if payload.collection_id is not None:
        collection = live_collection(session, payload.collection_id)
        changed = (
            set_membership(
                session, asset, collection, payload.collection_action == "add", request_id
            )
            or changed
        )
    return "updated" if changed else "unchanged"
