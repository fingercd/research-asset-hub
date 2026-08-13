"""领域路由：抽取、索引与作业。"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Header, Query, Request, Response
from sqlalchemy import func, select

from apps.api import routes_v1 as _v1
from apps.api.events import record_mutation
from apps.api.pagination import decode_cursor
from apps.api.problem import conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _etag,
    _job_representation_id,
    _list_payload,
    _request_id,
    _resolve_asset_redirect,
    _serialize_extraction,
    _serialize_index_state,
    router,
)
from apps.api.serialization import serialize_job
from packages.domain.enums import AuditAction, JobStatus
from packages.domain.models import Activity, DocumentChunk, IndexState, Job, Representation


@router.get("/extractions")
def list_extractions(
    request: Request,
    representation_id: UUID | None = None,
    asset_id: UUID | None = None,
    status: str | None = Query(
        None,
        pattern="^(pending|running|succeeded|failed|cancelled)$",
    ),
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        if asset_id is not None:
            asset_id, _ = _resolve_asset_redirect(session, asset_id)
        statement = (
            select(Job)
            .where(Job.job_type.in_(["extract_representation", "representation.extract"]))
            .order_by(Job.created_at.desc(), Job.id)
        )
        if status is not None:
            statement = statement.where(Job.status == status)
        jobs = list(session.scalars(statement).all())
        job_representation_ids = {
            item.id: value for item in jobs if (value := _job_representation_id(item)) is not None
        }
        representation_ids = list(set(job_representation_ids.values()))
        representations = {
            item.id: item
            for item in (
                session.scalars(
                    select(Representation).where(Representation.id.in_(representation_ids))
                ).all()
                if representation_ids
                else []
            )
        }
        filtered: list[Job] = []
        for job in jobs:
            current_representation_id = job_representation_ids.get(job.id)
            representation = (
                representations.get(current_representation_id)
                if current_representation_id is not None
                else None
            )
            if representation_id is not None and current_representation_id != representation_id:
                continue
            if asset_id is not None and (
                representation is None or representation.asset_id != asset_id
            ):
                continue
            filtered.append(job)
        page = filtered[offset : offset + PAGE_SIZE]
        page_ids = [item.id for item in page]
        activities: dict[UUID, Activity] = {}
        if page_ids:
            for activity in session.scalars(
                select(Activity)
                .where(
                    Activity.job_id.in_(page_ids),
                    Activity.activity_type == "extraction",
                )
                .order_by(Activity.started_at.desc(), Activity.id.desc())
            ).all():
                if activity.job_id is not None:
                    activities.setdefault(activity.job_id, activity)
        page_representation_ids = {
            value for job in page if (value := job_representation_ids.get(job.id)) is not None
        }
        chunk_counts: dict[UUID, int] = {}
        if page_representation_ids:
            chunk_counts = {
                current_representation_id: int(count)
                for current_representation_id, count in session.execute(
                    select(
                        DocumentChunk.representation_id,
                        func.count(DocumentChunk.id),
                    )
                    .where(DocumentChunk.representation_id.in_(page_representation_ids))
                    .group_by(DocumentChunk.representation_id)
                ).all()
            }
        serialized_extractions: list[dict[str, Any]] = []
        for job in page:
            current_representation_id = job_representation_ids.get(job.id)
            serialized_extractions.append(
                _serialize_extraction(
                    job,
                    representation=(
                        representations.get(current_representation_id)
                        if current_representation_id is not None
                        else None
                    ),
                    activity=activities.get(job.id),
                    chunk_count=(
                        chunk_counts.get(current_representation_id, 0)
                        if current_representation_id is not None
                        else 0
                    ),
                )
            )
        payload = _list_payload(
            serialized_extractions,
            offset,
            len(filtered),
        )
        payload["counts"] = dict(Counter(item.status for item in filtered))
        return payload

@router.get("/extractions/{job_id}")
def get_extraction(
    job_id: UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        job = session.get(Job, job_id)
        if job is None or job.job_type not in {
            "extract_representation",
            "representation.extract",
        }:
            raise not_found("Extraction", job_id)
        representation_id = _job_representation_id(job)
        representation = (
            session.get(Representation, representation_id)
            if representation_id is not None
            else None
        )
        activity = session.scalar(
            select(Activity)
            .where(
                Activity.job_id == job.id,
                Activity.activity_type == "extraction",
            )
            .order_by(Activity.started_at.desc(), Activity.id.desc())
        )
        chunk_count = 0
        if representation_id is not None:
            chunk_count = int(
                session.scalar(
                    select(func.count(DocumentChunk.id)).where(
                        DocumentChunk.representation_id == representation_id
                    )
                )
                or 0
            )
        result = _serialize_extraction(
            job,
            representation=representation,
            activity=activity,
            chunk_count=chunk_count,
        )
        response.headers["ETag"] = _etag(job.row_version)
        return result

@router.get("/indexes")
def list_indexes(
    request: Request,
    index_name: str | None = None,
    index_kind: str | None = Query(None, pattern="^(keyword|vector|code)$"),
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    status: str | None = Query(
        None,
        pattern="^(pending|indexing|ready|stale|failed)$",
    ),
    version: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        criteria: list[Any] = []
        if index_name is not None:
            criteria.append(IndexState.index_name == index_name)
        if index_kind is not None:
            criteria.append(IndexState.index_kind == index_kind)
        if entity_type is not None:
            criteria.append(IndexState.entity_type == entity_type)
        if entity_id is not None:
            criteria.append(IndexState.entity_id == entity_id)
        if status is not None:
            criteria.append(IndexState.status == status)
        if version is not None:
            criteria.append(IndexState.version == version)
        total = int(session.scalar(select(func.count(IndexState.id)).where(*criteria)) or 0)
        page = list(
            session.scalars(
                select(IndexState)
                .where(*criteria)
                .order_by(IndexState.updated_at.desc(), IndexState.id)
                .offset(offset)
                .limit(PAGE_SIZE)
            ).all()
        )
        payload = _list_payload(
            [_serialize_index_state(item) for item in page],
            offset,
            total,
        )
        payload["counts"] = {
            "by_status": {
                str(key): int(count)
                for key, count in session.execute(
                    select(IndexState.status, func.count(IndexState.id))
                    .where(*criteria)
                    .group_by(IndexState.status)
                ).all()
            },
            "by_index": {
                str(key): int(count)
                for key, count in session.execute(
                    select(IndexState.index_name, func.count(IndexState.id))
                    .where(*criteria)
                    .group_by(IndexState.index_name)
                ).all()
            },
            "by_version": {
                str(key): int(count)
                for key, count in session.execute(
                    select(IndexState.version, func.count(IndexState.id))
                    .where(*criteria)
                    .group_by(IndexState.version)
                ).all()
            },
        }
        return payload

@router.get("/indexes/{index_state_id}")
def get_index_state(
    index_state_id: UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = session.get(IndexState, index_state_id)
        if item is None:
            raise not_found("IndexState", index_state_id)
        result = _serialize_index_state(item)
        response.headers["ETag"] = _etag(item.row_version)
        return result

@router.get("/jobs")
def list_jobs(
    request: Request,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        statement = select(Job).order_by(Job.created_at.desc())
        if status:
            statement = statement.where(Job.status == status)
        jobs = list(session.scalars(statement).all())
        page = jobs[offset : offset + PAGE_SIZE]
        return _list_payload([serialize_job(item) for item in page], offset, len(jobs))

@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise not_found("Job", job_id)
        _check_if_match(if_match, job.row_version)
        if job.status not in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            raise conflict("Only failed or cancelled jobs can be retried.")
        before = serialize_job(job)
        job.status = JobStatus.PENDING.value
        job.progress = 0.0
        job.max_attempts = max(job.max_attempts, job.attempts + 1)
        job.cancel_requested = False
        job.error = None
        job.finished_at = None
        job.scheduled_at = datetime.now(UTC)
        job.lease_owner = None
        job.lease_until = None
        session.flush()
        result = serialize_job(job)
        record_mutation(
            session,
            entity_type="job",
            entity_id=job.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="job.retried",
        )
        response.headers["ETag"] = _etag(job.row_version)
        return result

@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise not_found("Job", job_id)
        _check_if_match(if_match, job.row_version)
        if job.status in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}:
            raise conflict("A finished job cannot be cancelled.")
        before = serialize_job(job)
        if job.status == JobStatus.PENDING.value:
            job.status = JobStatus.CANCELLED.value
            job.finished_at = datetime.now(UTC)
        else:
            job.cancel_requested = True
        session.flush()
        result = serialize_job(job)
        record_mutation(
            session,
            entity_type="job",
            entity_id=job.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="job.cancel-requested",
        )
        response.headers["ETag"] = _etag(job.row_version)
        return result
