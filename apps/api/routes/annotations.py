"""领域路由：批注。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Header, Request, Response
from sqlalchemy import func, select

from apps.api import routes_v1 as _v1
from apps.api.contracts import AnnotationCreate, AnnotationPatch
from apps.api.events import record_mutation, replay_idempotent
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _etag,
    _list_payload,
    _live_annotation,
    _live_asset,
    _request_id,
    _resolve_asset_redirect,
    router,
)
from apps.api.serialization import serialize_annotation, serialize_annotation_revision
from packages.domain.enums import AuditAction
from packages.domain.models import Annotation, AnnotationRevision, Representation


@router.get("/annotations")
def list_annotations(
    request: Request,
    asset_id: UUID | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        if asset_id is not None:
            asset_id, _ = _resolve_asset_redirect(session, asset_id)
        statement = (
            select(Annotation)
            .where(Annotation.deleted_at.is_(None))
            .order_by(Annotation.pinned.desc(), Annotation.updated_at.desc())
        )
        if asset_id is not None:
            statement = statement.where(Annotation.asset_id == asset_id)
        all_items = list(session.scalars(statement).all())
        page = all_items[offset : offset + PAGE_SIZE]
        return _list_payload(
            [serialize_annotation(item) for item in page],
            offset,
            len(all_items),
        )

@router.post("/annotations", status_code=201)
def create_annotation(
    payload: AnnotationCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _v1._database(request).session() as session:
        asset = _live_asset(session, payload.asset_id)
        if payload.representation_id is not None:
            representation = session.get(Representation, payload.representation_id)
            if (
                representation is None
                or representation.deleted_at is not None
                or representation.asset_id != asset.id
            ):
                raise ApiProblem(
                    422,
                    "表示层无效",
                    "The representation does not belong to the annotation asset.",
                    problem_type="urn:research-asset-hub:problem:validation",
                )
        replay = replay_idempotent(
            session,
            scope="create-annotation",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        annotation_values = payload.model_dump()
        annotation_values["asset_id"] = asset.id
        annotation = Annotation(**annotation_values)
        session.add(annotation)
        session.flush()
        result = serialize_annotation(annotation)
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="annotation.created",
            idempotency_scope="create-annotation",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(annotation.row_version)
        return result

@router.patch("/annotations/{annotation_id}")
def update_annotation(
    annotation_id: UUID,
    payload: AnnotationPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        annotation = _live_annotation(session, annotation_id)
        _check_if_match(if_match, annotation.row_version, required=True)
        before = serialize_annotation(annotation)
        current_revision = (
            session.scalar(
                select(func.max(AnnotationRevision.revision)).where(
                    AnnotationRevision.annotation_id == annotation.id
                )
            )
            or 0
        )
        session.add(
            AnnotationRevision(
                annotation_id=annotation.id,
                revision=current_revision + 1,
                title=annotation.title,
                summary=annotation.summary,
                body_markdown=annotation.body_markdown,
                anchor=annotation.anchor,
            )
        )
        if payload.representation_id is not None:
            representation = session.get(Representation, payload.representation_id)
            if (
                representation is None
                or representation.deleted_at is not None
                or representation.asset_id != annotation.asset_id
            ):
                raise ApiProblem(
                    422,
                    "表示层无效",
                    "The representation does not belong to the annotation asset.",
                    problem_type="urn:research-asset-hub:problem:validation",
                )
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(annotation, key, value)
        session.flush()
        result = serialize_annotation(annotation)
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="annotation.updated",
        )
        response.headers["ETag"] = _etag(annotation.row_version)
        return result

@router.delete("/annotations/{annotation_id}", status_code=204)
def delete_annotation(
    annotation_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _v1._database(request).session() as session:
        annotation = _live_annotation(session, annotation_id)
        _check_if_match(if_match, annotation.row_version)
        before = serialize_annotation(annotation)
        annotation.deleted_at = datetime.now(UTC)
        session.flush()
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.DELETE,
            before=before,
            after=serialize_annotation(annotation),
            request_id=_request_id(request),
            event_type="annotation.deleted",
        )
    return Response(status_code=204)

@router.get("/annotations/{annotation_id}/revisions")
def annotation_revisions(annotation_id: UUID, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        _live_annotation(session, annotation_id, include_deleted=True)
        revisions = list(
            session.scalars(
                select(AnnotationRevision)
                .where(AnnotationRevision.annotation_id == annotation_id)
                .order_by(AnnotationRevision.revision.desc())
            ).all()
        )
        return {
            "items": [serialize_annotation_revision(item) for item in revisions],
            "next_cursor": None,
            "total": len(revisions),
        }

@router.post("/annotations/{annotation_id}/revisions/{revision_number}/restore")
def restore_annotation_revision(
    annotation_id: UUID,
    revision_number: int,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        annotation = _live_annotation(session, annotation_id, include_deleted=True)
        _check_if_match(if_match, annotation.row_version)
        revision = session.scalar(
            select(AnnotationRevision).where(
                AnnotationRevision.annotation_id == annotation_id,
                AnnotationRevision.revision == revision_number,
            )
        )
        if revision is None:
            raise not_found("Annotation revision", revision_number)
        before = serialize_annotation(annotation)
        current_revision = (
            session.scalar(
                select(func.max(AnnotationRevision.revision)).where(
                    AnnotationRevision.annotation_id == annotation.id
                )
            )
            or 0
        )
        session.add(
            AnnotationRevision(
                annotation_id=annotation.id,
                revision=current_revision + 1,
                title=annotation.title,
                summary=annotation.summary,
                body_markdown=annotation.body_markdown,
                anchor=annotation.anchor,
                source="restore",
            )
        )
        annotation.title = revision.title
        annotation.summary = revision.summary
        annotation.body_markdown = revision.body_markdown
        annotation.anchor = revision.anchor
        annotation.deleted_at = None
        session.flush()
        result = serialize_annotation(annotation)
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="annotation.revision-restored",
        )
        response.headers["ETag"] = _etag(annotation.row_version)
        return result
