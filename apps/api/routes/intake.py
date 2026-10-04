from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import routes_v1 as _v1
from apps.api.problem import ApiProblem, not_found
from packages.domain.product_models import IntakeDraft
from packages.intake.contracts import CommitRequest, DraftCreate, DraftPatch
from packages.intake.service import (
    IntakeError,
    checked_reference,
    complete_commit,
    draft_directory,
    editable,
    prepare_commit,
    preview,
    require_domain,
    serialize_draft,
    validate_pdf,
)
from packages.managed_sources.service import data_directory, file_lock, managed_path


def _draft(session: Session, draft_id: UUID) -> IntakeDraft:
    row = session.get(IntakeDraft, draft_id)
    if row is None:
        raise not_found("Intake draft", draft_id)
    return row


def _problem(error: IntakeError) -> ApiProblem:
    return ApiProblem(error.status, "资料入库未完成", str(error), code="intake_invalid")


def _lock(settings: Any, draft_id: UUID) -> Any:
    return file_lock(managed_path(data_directory(settings), "locks", f"intake-{draft_id}.lock"))


@_v1.router.post("/intake/drafts", status_code=201)
def create_draft(payload: DraftCreate, request: Request) -> dict[str, Any]:
    try:
        with _v1._database(request).session() as session:
            require_domain(session, _v1._settings(request), payload.domain)
            draft = IntakeDraft(
                domain=payload.domain,
                storage_mode=payload.storage_mode,
                metadata_json=payload.model_dump(mode="json", exclude={"domain", "storage_mode"}),
            )
            session.add(draft)
            session.flush()
            return serialize_draft(draft)
    except IntakeError as error:
        raise _problem(error) from error


@_v1.router.get("/intake/drafts")
def list_drafts(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        rows = session.scalars(
            select(IntakeDraft)
            .where(IntakeDraft.status != "cancelled")
            .order_by(IntakeDraft.created_at.desc())
            .limit(limit)
        ).all()
        return {
            "items": [serialize_draft(row) for row in rows],
            "next_cursor": None,
            "total": len(rows),
        }


@_v1.router.get("/intake/drafts/{draft_id}")
def get_draft(draft_id: UUID, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        return serialize_draft(_draft(session, draft_id))


@_v1.router.patch("/intake/drafts/{draft_id}")
def update_draft(draft_id: UUID, payload: DraftPatch, request: Request) -> dict[str, Any]:
    settings = _v1._settings(request)
    try:
        with _lock(settings, draft_id), _v1._database(request).session() as session:
            draft = _draft(session, draft_id)
            editable(draft)
            values = payload.model_dump(mode="json", exclude_unset=True)
            if "domain" in values:
                require_domain(session, settings, values["domain"])
                draft.domain = values.pop("domain")
            if "reference_path" in values:
                if draft.storage_mode != "reference":
                    raise IntakeError("托管草稿通过 PDF 上传添加文件")
                path = checked_reference(settings, values.pop("reference_path") or "")
                draft.pdf_sha256, draft.pdf_size_bytes = validate_pdf(
                    path, settings.intake_max_pdf_bytes
                )
                draft.reference_path, draft.filename, draft.status = str(path), path.name, "ready"
            draft.metadata_json = {**draft.metadata_json, **values}
            session.flush()
            return serialize_draft(draft)
    except IntakeError as error:
        raise _problem(error) from error


@_v1.router.put("/intake/drafts/{draft_id}/pdf")
async def upload_pdf(
    draft_id: UUID, request: Request, filename: str = Query(default="paper.pdf", max_length=255)
) -> dict[str, Any]:
    settings = _v1._settings(request)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/pdf":
        raise ApiProblem(
            415, "文件格式无效", "PDF 上传必须使用 application/pdf", code="intake_pdf_media_type"
        )
    if (
        not filename
        or Path(filename).name != filename
        or any(char in filename for char in "\\/:\0")
    ):
        raise ApiProblem(
            422, "文件名称无效", "请选择单个 PDF 文件名称", code="intake_filename_invalid"
        )
    temporary: Path | None = None
    published: Path | None = None
    try:
        with _lock(settings, draft_id), _v1._database(request).session() as session:
            draft = _draft(session, draft_id)
            editable(draft)
            if draft.storage_mode != "managed":
                raise IntakeError("原位引用草稿请明确选择永久 PDF 路径")
            directory = draft_directory(settings, draft_id)
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f"upload-{uuid4().hex}.tmp"
            length = 0
            with temporary.open("xb") as handle:
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > settings.intake_max_pdf_bytes:
                        raise IntakeError("PDF 超过上传大小限制", 413)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            pdf_hash, size = validate_pdf(temporary, settings.intake_max_pdf_bytes)
            destination = managed_path(
                data_directory(settings), "library", "staging", str(draft_id), f"{pdf_hash}.pdf"
            )
            if destination.exists():
                if validate_pdf(destination, settings.intake_max_pdf_bytes)[0] != pdf_hash:
                    raise IntakeError("草稿文件指纹不一致", 409)
            else:
                os.replace(temporary, destination)
                published = destination
            draft.pdf_sha256, draft.pdf_size_bytes, draft.filename, draft.status = (
                pdf_hash,
                size,
                filename,
                "ready",
            )
            session.flush()
            result = serialize_draft(draft)
        return result
    except IntakeError as error:
        if published:
            published.unlink(missing_ok=True)
        raise _problem(error) from error
    except Exception:
        if published:
            published.unlink(missing_ok=True)
        raise
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@_v1.router.get("/intake/drafts/{draft_id}/preview")
def preview_draft(draft_id: UUID, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        return preview(session, _draft(session, draft_id))


@_v1.router.post("/intake/drafts/{draft_id}/commit")
def commit_draft(draft_id: UUID, payload: CommitRequest, request: Request) -> dict[str, Any]:
    settings = _v1._settings(request)
    plan = payload.model_dump(mode="json")
    try:
        with (
            _lock(settings, draft_id),
            file_lock(managed_path(data_directory(settings), "locks", "intake-commits.lock")),
        ):
            # Persist the user's decision before publishing immutable files. A crash
            # can retry this exact plan, but cannot mutate already frozen input.
            with _v1._database(request).session() as session:
                prepare_commit(session, settings, _draft(session, draft_id), plan)
            try:
                with _v1._database(request).session() as session:
                    result = complete_commit(
                        session, settings, _draft(session, draft_id), _v1._request_id(request)
                    )
            except Exception:
                with _v1._database(request).session() as session:
                    draft = _draft(session, draft_id)
                    draft.error = "提交未完成；输入已保留，使用原提交决定重试即可"
                raise
            directory = draft_directory(settings, draft_id)
            if directory.exists():
                shutil.rmtree(directory)
            return result
    except IntakeError as error:
        raise _problem(error) from error


@_v1.router.delete("/intake/drafts/{draft_id}")
def cancel_draft(draft_id: UUID, request: Request) -> dict[str, Any]:
    settings = _v1._settings(request)
    try:
        with _lock(settings, draft_id):
            with _v1._database(request).session() as session:
                draft = _draft(session, draft_id)
                if draft.status != "cancelled":
                    editable(draft)
                    draft.status = "cancelled"
            directory = draft_directory(settings, draft_id)
            if directory.exists():
                shutil.rmtree(directory)
            return {"draft_id": str(draft_id), "status": "cancelled"}
    except IntakeError as error:
        raise _problem(error) from error
