"""User-declared paper/code links and explicitly requested public snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.api import routes_v1 as v1
from apps.api.events import record_mutation, replay_idempotent
from apps.api.problem import ApiProblem
from packages.domain.enums import AuditAction
from packages.repository_snapshots.download import SnapshotError
from packages.repository_snapshots.service import link_code, queue_snapshot


class CodeLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_id: UUID
    url: str | None = Field(None, max_length=2048)
    existing_asset_id: UUID | None = None
    local_directory: str | None = Field(None, max_length=4096)
    kind: Literal["official", "reproduction", "project"] = "official"
    download: bool = False
    revision: str | None = Field(None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def has_source(self) -> CodeLinkRequest:
        if not (self.url or self.existing_asset_id or self.local_directory):
            raise ValueError("Choose a code URL, existing repository asset or local directory.")
        return self


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_asset_id: UUID
    revision: str | None = Field(None, min_length=1, max_length=255)


@v1.router.post("/code-links", status_code=201)
def create_code_link(
    payload: CodeLinkRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, str]:
    values = payload.model_dump(mode="json")
    with v1._database(request).session() as session:
        replay = replay_idempotent(
            session, scope="code-link", key=idempotency_key, request_payload=values
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        try:
            result = link_code(
                session,
                **payload.model_dump(),
                data_dir=Path(v1._settings(request).data_dir),
                request_id=v1._request_id(request),
            )
        except (SnapshotError, OSError) as error:
            raise ApiProblem(422, "代码关联无法建立", str(error)) from error
        if idempotency_key:
            record_mutation(
                session,
                entity_type="relation",
                entity_id=UUID(result["relation_id"]),
                action=AuditAction.CREATE,
                before=None,
                after=result,
                request_id=v1._request_id(request),
                event_type="code-link.requested",
                idempotency_scope="code-link",
                idempotency_key=idempotency_key,
                request_payload=values,
                response_payload=result,
            )
    v1._source_registry(request).refresh_if_changed()
    return result


@v1.router.post("/repository-snapshots", status_code=202)
def create_repository_snapshot(
    payload: SnapshotRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, str]:
    values = payload.model_dump(mode="json")
    with v1._database(request).session() as session:
        replay = replay_idempotent(
            session, scope="repository-snapshot", key=idempotency_key, request_payload=values
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        try:
            job = queue_snapshot(
                session, payload.repository_asset_id, payload.revision, v1._request_id(request)
            )
        except SnapshotError as error:
            raise ApiProblem(422, "代码快照无法下载", str(error)) from error
        result = {"job_id": str(job.id)}
        if idempotency_key:
            record_mutation(
                session,
                entity_type="job",
                entity_id=job.id,
                action=AuditAction.CREATE,
                before=None,
                after=result,
                request_id=v1._request_id(request),
                event_type="repository-snapshot.requested",
                idempotency_scope="repository-snapshot",
                idempotency_key=idempotency_key,
                request_payload=values,
                response_payload=result,
            )
        return result
