"""领域路由：字段覆盖。"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Header, Request, Response
from sqlalchemy import select

from apps.api import routes_v1 as _v1
from apps.api.contracts import OverrideCreate, OverridePatch
from apps.api.events import record_mutation, replay_idempotent
from apps.api.pagination import decode_cursor
from apps.api.problem import conflict
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _current_source_claim,
    _ensure_override_base_claim,
    _etag,
    _list_payload,
    _live_asset,
    _live_override,
    _override_asset_field,
    _override_base_claim,
    _project_override_value,
    _record_asset_projection,
    _request_id,
    _resolve_asset_redirect,
    _serialize_override,
    _validated_override_value,
    router,
)
from apps.api.serialization import serialize_asset
from packages.domain.enums import AuditAction, FieldResolutionKind
from packages.domain.models import FieldResolution


@router.get("/overrides")
def list_overrides(
    request: Request,
    entity_id: UUID | None = None,
    field_path: str | None = None,
    include_cleared: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        if entity_id is not None:
            entity_id, _ = _resolve_asset_redirect(session, entity_id)
        kinds = [FieldResolutionKind.OVERRIDE.value]
        if include_cleared:
            kinds.append(FieldResolutionKind.CLEARED.value)
        statement = (
            select(FieldResolution)
            .where(
                FieldResolution.entity_type == "asset",
                FieldResolution.resolution_kind.in_(kinds),
            )
            .order_by(FieldResolution.updated_at.desc(), FieldResolution.id)
        )
        if entity_id is not None:
            statement = statement.where(FieldResolution.entity_id == entity_id)
        if field_path is not None:
            statement = statement.where(FieldResolution.field_path == field_path)
        all_items = list(session.scalars(statement).all())
        page = all_items[offset : offset + PAGE_SIZE]
        return _list_payload(
            [_serialize_override(item) for item in page],
            offset,
            len(all_items),
        )

@router.post("/overrides", status_code=201)
def create_override(
    payload: OverrideCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _v1._database(request).session() as session:
        _override_asset_field(payload.field_path)
        replay = replay_idempotent(
            session,
            scope="create-override",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["ETag"] = _etag(int(replay["row_version"]))
            return replay
        asset = _live_asset(session, payload.entity_id)
        asset_before = serialize_asset(asset)
        override_value = _validated_override_value(
            _override_asset_field(payload.field_path),
            payload.override_value,
        )
        item = session.scalar(
            select(FieldResolution).where(
                FieldResolution.entity_type == payload.entity_type,
                FieldResolution.entity_id == asset.id,
                FieldResolution.field_path == payload.field_path,
            )
        )
        before: dict[str, Any] | None = None
        action = AuditAction.CREATE
        if item is not None:
            if item.resolution_kind == FieldResolutionKind.OVERRIDE.value:
                raise conflict(
                    f"An active override already exists as {item.id}; update it with PATCH."
                )
            before = {
                "id": str(item.id),
                "entity_type": item.entity_type,
                "entity_id": str(item.entity_id),
                "field_path": item.field_path,
                "resolution_kind": item.resolution_kind,
                "selected_claim_id": (
                    str(item.selected_claim_id) if item.selected_claim_id is not None else None
                ),
                "override_value": item.override_value,
                "reason": item.reason,
                "row_version": item.row_version,
            }
            action = (
                AuditAction.RESTORE
                if item.resolution_kind == FieldResolutionKind.CLEARED.value
                else AuditAction.UPDATE
            )
            item.resolution_kind = FieldResolutionKind.OVERRIDE.value
            item.selected_claim_id = None
            item.override_value = override_value
            item.reason = payload.reason
            item.resolved_by = "local"
        else:
            item = FieldResolution(
                entity_type=payload.entity_type,
                entity_id=asset.id,
                field_path=payload.field_path,
                resolution_kind=FieldResolutionKind.OVERRIDE.value,
                selected_claim_id=None,
                override_value=override_value,
                reason=payload.reason,
                resolved_by="local",
            )
            session.add(item)
        session.flush()
        _ensure_override_base_claim(session, item, asset)
        item.resolution_kind = FieldResolutionKind.OVERRIDE.value
        item.selected_claim_id = None
        item.override_value = override_value
        item.reason = payload.reason
        item.resolved_by = "local"
        _project_override_value(
            asset,
            field_path=item.field_path,
            value=item.override_value,
        )
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-projected",
        )
        result = _serialize_override(item)
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=action,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="field-override.created",
            idempotency_scope="create-override",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result

@router.get("/overrides/{override_id}")
def get_override(
    override_id: UUID,
    request: Request,
    response: Response,
    include_cleared: bool = False,
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_override(
            session,
            override_id,
            include_deleted=include_cleared,
        )
        result = _serialize_override(item)
        response.headers["ETag"] = _etag(item.row_version)
        return result

@router.patch("/overrides/{override_id}")
def update_override(
    override_id: UUID,
    payload: OverridePatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_override(session, override_id)
        _check_if_match(if_match, item.row_version, required=True)
        before = _serialize_override(item)
        asset = _live_asset(session, item.entity_id)
        asset_before = serialize_asset(asset)
        changes = payload.model_dump(exclude_unset=True)
        if "override_value" in changes:
            item.override_value = _validated_override_value(
                _override_asset_field(item.field_path),
                changes["override_value"],
            )
            _project_override_value(
                asset,
                field_path=item.field_path,
                value=item.override_value,
            )
        if "reason" in changes:
            item.reason = changes["reason"]
        item.resolved_by = "local"
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-projected",
        )
        result = _serialize_override(item)
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="field-override.updated",
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result

@router.delete("/overrides/{override_id}", status_code=204)
def delete_override(
    override_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _v1._database(request).session() as session:
        item = _live_override(session, override_id)
        _check_if_match(if_match, item.row_version)
        before = _serialize_override(item)
        asset = _live_asset(session, item.entity_id)
        asset_before = serialize_asset(asset)
        base_claim = _current_source_claim(session, item)
        if base_claim is None:
            raise conflict(
                "The override has no active source or recoverable base claim and "
                "cannot be safely cleared."
            )
        _project_override_value(
            asset,
            field_path=item.field_path,
            value=base_claim.value_json,
        )
        item.resolution_kind = FieldResolutionKind.CLEARED.value
        item.resolved_by = "local"
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-cleared",
        )
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=AuditAction.DELETE,
            before=before,
            after=_serialize_override(item),
            request_id=_request_id(request),
            event_type="field-override.cleared",
        )
    return Response(status_code=204)

@router.post("/overrides/{override_id}/restore")
def restore_override(
    override_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_override(session, override_id, include_deleted=True)
        _check_if_match(if_match, item.row_version)
        if item.resolution_kind == FieldResolutionKind.OVERRIDE.value:
            result = _serialize_override(item)
            response.headers["ETag"] = _etag(item.row_version)
            return result
        before = _serialize_override(item)
        asset = _live_asset(session, item.entity_id)
        asset_before = serialize_asset(asset)
        if _override_base_claim(session, item) is None:
            raise conflict(
                "The override has no recoverable base claim and cannot be safely restored."
            )
        _project_override_value(
            asset,
            field_path=item.field_path,
            value=item.override_value,
        )
        item.resolution_kind = FieldResolutionKind.OVERRIDE.value
        item.resolved_by = "local"
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-restored",
        )
        result = _serialize_override(item)
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="field-override.restored",
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result
