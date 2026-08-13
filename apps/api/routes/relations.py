"""领域路由：资产关系。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Header, Query, Request, Response
from sqlalchemy import func, or_, select

from apps.api import routes_v1 as _v1
from apps.api.contracts import RelationCreate, RelationEvidenceCreate, RelationPatch
from apps.api.events import record_mutation, replay_idempotent, stable_hash
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _etag,
    _list_payload,
    _live_asset,
    _live_relation,
    _relation_duplicate,
    _request_id,
    _resolve_asset_redirect,
    _serialize_relation,
    _serialize_relation_evidence,
    router,
)
from packages.domain.enums import AuditAction
from packages.domain.models import Relation, RelationEvidenceModel, SourceRecordVersion


@router.get("/relations")
def list_relations(
    request: Request,
    subject_asset_id: UUID | None = None,
    object_asset_id: UUID | None = None,
    asset_id: UUID | None = None,
    relation_type: str | None = None,
    confirmation_status: str | None = Query(
        None,
        pattern="^(candidate|confirmed|rejected)$",
    ),
    include_deleted: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        if subject_asset_id is not None:
            subject_asset_id, _ = _resolve_asset_redirect(
                session,
                subject_asset_id,
            )
        if object_asset_id is not None:
            object_asset_id, _ = _resolve_asset_redirect(
                session,
                object_asset_id,
            )
        if asset_id is not None:
            asset_id, _ = _resolve_asset_redirect(session, asset_id)
        statement = select(Relation).order_by(Relation.updated_at.desc(), Relation.id)
        if not include_deleted:
            statement = statement.where(Relation.deleted_at.is_(None))
        if subject_asset_id is not None:
            statement = statement.where(Relation.subject_asset_id == subject_asset_id)
        if object_asset_id is not None:
            statement = statement.where(Relation.object_asset_id == object_asset_id)
        if asset_id is not None:
            statement = statement.where(
                or_(
                    Relation.subject_asset_id == asset_id,
                    Relation.object_asset_id == asset_id,
                )
            )
        if relation_type is not None:
            statement = statement.where(Relation.relation_type == relation_type)
        if confirmation_status is not None:
            statement = statement.where(Relation.confirmation_status == confirmation_status)
        all_items = list(session.scalars(statement).all())
        page = all_items[offset : offset + PAGE_SIZE]
        relation_ids = [item.id for item in page]
        evidence_counts: dict[UUID, int] = {}
        if relation_ids:
            evidence_counts = {
                relation_id: int(count)
                for relation_id, count in session.execute(
                    select(
                        RelationEvidenceModel.relation_id,
                        func.count(RelationEvidenceModel.id),
                    )
                    .where(RelationEvidenceModel.relation_id.in_(relation_ids))
                    .group_by(RelationEvidenceModel.relation_id)
                ).all()
            }
        return _list_payload(
            [
                _serialize_relation(
                    item,
                    evidence_count=evidence_counts.get(item.id, 0),
                )
                for item in page
            ],
            offset,
            len(all_items),
        )

@router.post("/relations", status_code=201)
def create_relation(
    payload: RelationCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _v1._database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="create-relation",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["ETag"] = _etag(int(replay["row_version"]))
            return replay
        subject_asset = _live_asset(session, payload.subject_asset_id)
        object_asset = _live_asset(session, payload.object_asset_id)
        relation_type = payload.relation_type.value
        duplicate = _relation_duplicate(
            session,
            subject_asset_id=subject_asset.id,
            relation_type=relation_type,
            object_asset_id=object_asset.id,
        )
        if duplicate is not None:
            state = "soft-deleted; restore it" if duplicate.deleted_at else "already active"
            raise conflict(
                f"The logical relation already exists ({duplicate.id}, {state}). "
                "Add evidence to the existing relation instead."
            )
        relation = Relation(
            subject_asset_id=subject_asset.id,
            object_asset_id=object_asset.id,
            relation_type=relation_type,
            source_key=payload.source_key,
            evidence={},
            confidence=payload.confidence,
            confirmation_status=payload.confirmation_status.value,
            attributes=payload.attributes,
        )
        session.add(relation)
        session.flush()
        result = _serialize_relation(relation, evidence_count=0)
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="relation.created",
            idempotency_scope="create-relation",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(relation.row_version)
        return result

@router.get("/relations/{relation_id}")
def get_relation(
    relation_id: UUID,
    request: Request,
    response: Response,
    include_deleted: bool = False,
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        relation = _live_relation(
            session,
            relation_id,
            include_deleted=include_deleted,
        )
        evidence_count = int(
            session.scalar(
                select(func.count(RelationEvidenceModel.id)).where(
                    RelationEvidenceModel.relation_id == relation.id
                )
            )
            or 0
        )
        result = _serialize_relation(relation, evidence_count=evidence_count)
        response.headers["ETag"] = _etag(relation.row_version)
        return result

@router.patch("/relations/{relation_id}")
def update_relation(
    relation_id: UUID,
    payload: RelationPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        relation = _live_relation(session, relation_id)
        _check_if_match(if_match, relation.row_version, required=True)
        subject_asset_id = (
            _live_asset(session, payload.subject_asset_id).id
            if payload.subject_asset_id is not None
            else relation.subject_asset_id
        )
        object_asset_id = (
            _live_asset(session, payload.object_asset_id).id
            if payload.object_asset_id is not None
            else relation.object_asset_id
        )
        if subject_asset_id == object_asset_id:
            raise ApiProblem(
                422,
                "关系端点无效",
                "A relation cannot connect an asset to itself.",
                problem_type="urn:research-asset-hub:problem:validation",
            )
        _live_asset(session, subject_asset_id)
        _live_asset(session, object_asset_id)
        relation_type = (
            payload.relation_type.value
            if payload.relation_type is not None
            else relation.relation_type
        )
        duplicate = _relation_duplicate(
            session,
            subject_asset_id=subject_asset_id,
            relation_type=relation_type,
            object_asset_id=object_asset_id,
            exclude_id=relation.id,
        )
        if duplicate is not None:
            raise conflict(
                f"Updating this relation would duplicate logical relation {duplicate.id}."
            )
        before = _serialize_relation(relation)
        changes = payload.model_dump(exclude_unset=True)
        changes["subject_asset_id"] = subject_asset_id
        changes["object_asset_id"] = object_asset_id
        for key, value in changes.items():
            if key in {"relation_type", "confirmation_status"} and value is not None:
                value = value.value
            setattr(relation, key, value)
        session.flush()
        evidence_count = int(
            session.scalar(
                select(func.count(RelationEvidenceModel.id)).where(
                    RelationEvidenceModel.relation_id == relation.id
                )
            )
            or 0
        )
        result = _serialize_relation(relation, evidence_count=evidence_count)
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="relation.updated",
        )
        response.headers["ETag"] = _etag(relation.row_version)
        return result

@router.delete("/relations/{relation_id}", status_code=204)
def delete_relation(
    relation_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _v1._database(request).session() as session:
        relation = _live_relation(session, relation_id)
        _check_if_match(if_match, relation.row_version)
        before = _serialize_relation(relation)
        relation.deleted_at = datetime.now(UTC)
        session.flush()
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.DELETE,
            before=before,
            after=_serialize_relation(relation),
            request_id=_request_id(request),
            event_type="relation.deleted",
        )
    return Response(status_code=204)

@router.post("/relations/{relation_id}/restore")
def restore_relation(
    relation_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        relation = _live_relation(session, relation_id, include_deleted=True)
        _check_if_match(if_match, relation.row_version)
        if relation.deleted_at is None:
            result = _serialize_relation(relation)
            response.headers["ETag"] = _etag(relation.row_version)
            return result
        duplicate = _relation_duplicate(
            session,
            subject_asset_id=relation.subject_asset_id,
            relation_type=relation.relation_type,
            object_asset_id=relation.object_asset_id,
            exclude_id=relation.id,
        )
        if duplicate is not None:
            raise conflict(
                f"Relation {duplicate.id} now owns the same logical edge; "
                "the deleted relation cannot be restored."
            )
        before = _serialize_relation(relation)
        relation.deleted_at = None
        session.flush()
        result = _serialize_relation(relation)
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="relation.restored",
        )
        response.headers["ETag"] = _etag(relation.row_version)
        return result

@router.get("/relations/{relation_id}/evidence")
def list_relation_evidence(
    relation_id: UUID,
    request: Request,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        _live_relation(session, relation_id, include_deleted=True)
        all_items = list(
            session.scalars(
                select(RelationEvidenceModel)
                .where(RelationEvidenceModel.relation_id == relation_id)
                .order_by(
                    RelationEvidenceModel.created_at.desc(),
                    RelationEvidenceModel.id,
                )
            ).all()
        )
        page = all_items[offset : offset + PAGE_SIZE]
        return _list_payload(
            [_serialize_relation_evidence(item) for item in page],
            offset,
            len(all_items),
        )

@router.post("/relations/{relation_id}/evidence", status_code=201)
def create_relation_evidence(
    relation_id: UUID,
    payload: RelationEvidenceCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    scope = f"create-relation-evidence:{relation_id}"
    with _v1._database(request).session() as session:
        relation = _live_relation(session, relation_id)
        replay = replay_idempotent(
            session,
            scope=scope,
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        if (
            payload.source_record_version_id is not None
            and session.get(SourceRecordVersion, payload.source_record_version_id) is None
        ):
            raise not_found("SourceRecordVersion", payload.source_record_version_id)
        evidence_hash = stable_hash(
            {
                "source_record_version_id": payload.source_record_version_id,
                "provider": payload.provider,
                "source_key": payload.source_key,
                "evidence": payload.evidence,
                "confidence": payload.confidence,
            }
        )
        duplicate = session.scalar(
            select(RelationEvidenceModel).where(
                RelationEvidenceModel.relation_id == relation.id,
                RelationEvidenceModel.evidence_hash == evidence_hash,
            )
        )
        if duplicate is not None:
            raise conflict(f"Equivalent immutable evidence already exists as {duplicate.id}.")
        item = RelationEvidenceModel(
            relation_id=relation.id,
            source_record_version_id=payload.source_record_version_id,
            provider=payload.provider,
            source_key=payload.source_key,
            evidence=payload.evidence,
            evidence_hash=evidence_hash,
            confidence=payload.confidence,
            fetched_at=payload.fetched_at,
        )
        session.add(item)
        session.flush()
        result = _serialize_relation_evidence(item)
        record_mutation(
            session,
            entity_type="relation_evidence",
            entity_id=item.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="relation-evidence.created",
            idempotency_scope=scope,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        return result
