"""Collections, saved searches, bulk edits, reading positions and domain packages."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Header, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from apps.api import routes_v1 as _v1
from apps.api.catalog_queries import visible_clause
from apps.api.events import record_mutation, replay_idempotent
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _etag,
    _list_payload,
    _live_asset,
    _request_id,
    router,
)
from apps.api.serialization import serialize_asset
from packages.domain.enums import AuditAction
from packages.domain.models import Asset, Job, ReadingState, Relation, Representation, SavedView
from packages.domain.serving_scope import representation_serving_status
from packages.domain.workflow_models import PackageExport, PackageImport, ReadingPosition
from packages.domain_catalog.service import domain_slugs
from packages.workflows.contracts import (
    AssetSelection,
    BulkUpdate,
    CollectionCreate,
    CollectionPatch,
    PackageExportRequest,
    ReadingPositionUpdate,
    SavedSearchCreate,
    SavedSearchPatch,
)
from packages.workflows.package_contracts import PackageCommitRequest
from packages.workflows.packages import (
    MAX_ARCHIVE_BYTES,
    PackageError,
    commit_package,
    import_payload,
    package_directory,
    registered_package_path,
    stage_package,
)
from packages.workflows.service import apply_bulk_item, audit, live_collection, set_membership


def _validate_domains(session: Session, request: Request, names: list[str]) -> None:
    unknown = set(names) - domain_slugs(session, _v1._settings(request).taxonomy_config_path)
    if unknown:
        raise ApiProblem(422, "领域未注册", f"Unknown domains: {', '.join(sorted(unknown))}.")


def _collection_payload(session: Session, item: Asset) -> dict[str, Any]:
    count = (
        session.scalar(
            select(func.count())
            .select_from(Relation)
            .join(
                Asset,
                Asset.id == Relation.subject_asset_id,
            )
            .where(
                Relation.object_asset_id == item.id,
                Relation.relation_type == "belongs_to_collection",
                Relation.deleted_at.is_(None),
                visible_clause(session),
            )
        )
        or 0
    )
    return {
        "id": str(item.id),
        "label": item.title,
        "description": item.summary,
        "member_count": count,
        "row_version": item.row_version,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


@router.get("/collections")
def list_collections(request: Request, cursor: str | None = None) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        where = (Asset.asset_type == "collection", visible_clause(session))
        total = session.scalar(select(func.count()).select_from(Asset).where(*where)) or 0
        rows = session.scalars(
            select(Asset)
            .where(*where)
            .order_by(Asset.title, Asset.id)
            .offset(offset)
            .limit(PAGE_SIZE)
        )
        return _list_payload([_collection_payload(session, item) for item in rows], offset, total)


@router.post("/collections", status_code=201)
def create_collection(
    payload: CollectionCreate, request: Request, response: Response
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = Asset(asset_type="collection", title=payload.label, summary=payload.description)
        session.add(item)
        session.flush()
        result = _collection_payload(session, item)
        audit(session, item, "collection", None, result, _request_id(request))
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.get("/collections/{collection_id}")
def get_collection(collection_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = live_collection(session, collection_id)
        response.headers["ETag"] = _etag(item.row_version)
        return _collection_payload(session, item)


@router.patch("/collections/{collection_id}")
def update_collection(
    collection_id: UUID,
    payload: CollectionPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = live_collection(session, collection_id)
        _check_if_match(if_match, item.row_version, required=True)
        before = _collection_payload(session, item)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(item, "title" if key == "label" else "summary", value)
        session.flush()
        result = _collection_payload(session, item)
        audit(session, item, "collection", before, result, _request_id(request))
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.delete("/collections/{collection_id}", status_code=204)
def delete_collection(
    collection_id: UUID, request: Request, if_match: str | None = Header(None, alias="If-Match")
) -> Response:
    with _v1._database(request).session() as session:
        item = live_collection(session, collection_id)
        _check_if_match(if_match, item.row_version, required=True)
        before = _collection_payload(session, item)
        item.deleted_at = datetime.now(UTC)
        session.flush()
        audit(
            session,
            item,
            "collection",
            before,
            _collection_payload(session, item),
            _request_id(request),
            action=AuditAction.DELETE,
        )
    return Response(status_code=204)


@router.get("/collections/{collection_id}/members")
def list_collection_members(
    collection_id: UUID, request: Request, cursor: str | None = None
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        collection = live_collection(session, collection_id)
        member_ids = select(Relation.subject_asset_id).where(
            Relation.object_asset_id == collection.id,
            Relation.relation_type == "belongs_to_collection",
            Relation.deleted_at.is_(None),
        )
        where = (Asset.id.in_(member_ids), visible_clause(session))
        total = session.scalar(select(func.count()).select_from(Asset).where(*where)) or 0
        assets = list(
            session.scalars(
                select(Asset)
                .where(*where)
                .order_by(Asset.title, Asset.id)
                .offset(offset)
                .limit(PAGE_SIZE)
            )
        )
        states = {
            state.asset_id: state
            for state in session.scalars(
                select(ReadingState).where(
                    ReadingState.asset_id.in_([item.id for item in assets]),
                    ReadingState.user_id == "local",
                )
            )
        }
        return _list_payload(
            [serialize_asset(item, reading_state=states.get(item.id)) for item in assets],
            offset,
            total,
        )


def _item_results(
    request: Request, asset_ids: list[UUID], operation: Callable[[Session, UUID], str]
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        try:
            # Each asset owns its transaction: one invalid member cannot undo successful edits.
            with _v1._database(request).session() as session:
                status = operation(session, asset_id)
            results.append({"id": str(asset_id), "status": status})
        except ApiProblem as error:
            results.append(
                {
                    "id": str(asset_id),
                    "status": "failed",
                    "error": {"code": error.code, "message": error.detail},
                }
            )
        except SQLAlchemyError:
            results.append(
                {
                    "id": str(asset_id),
                    "status": "failed",
                    "error": {
                        "code": "write_conflict",
                        "message": "This item could not be saved; reload and retry.",
                    },
                }
            )
    failed = sum(item["status"] == "failed" for item in results)
    return {
        "items": results,
        "total": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
    }


def _change_members(
    collection_id: UUID, payload: AssetSelection, request: Request, add: bool
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        live_collection(session, collection_id)

    def operation(session: Session, asset_id: UUID) -> str:
        collection = live_collection(session, collection_id)
        asset = _live_asset(session, asset_id)
        session.refresh(asset, with_for_update=True)
        return (
            "updated"
            if set_membership(session, asset, collection, add, _request_id(request))
            else "unchanged"
        )

    return _item_results(request, payload.asset_ids, operation)


@router.post("/collections/{collection_id}/members")
def add_collection_members(
    collection_id: UUID, payload: AssetSelection, request: Request
) -> dict[str, Any]:
    return _change_members(collection_id, payload, request, True)


@router.delete("/collections/{collection_id}/members")
def remove_collection_members(
    collection_id: UUID, payload: AssetSelection, request: Request
) -> dict[str, Any]:
    return _change_members(collection_id, payload, request, False)


@router.post("/assets/bulk")
def bulk_update_assets(payload: BulkUpdate, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        _validate_domains(session, request, payload.add_domains + payload.remove_domains)
    return _item_results(
        request,
        payload.asset_ids,
        lambda session, asset_id: apply_bulk_item(session, asset_id, payload, _request_id(request)),
    )


def _saved_payload(item: SavedView) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "definition": item.definition,
        "row_version": item.row_version,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _live_search(session: Session, search_id: UUID) -> SavedView:
    item = session.get(SavedView, search_id)
    if (
        item is None
        or item.view_kind != "search"
        or item.user_id != "local"
        or item.deleted_at is not None
    ):
        raise not_found("Saved search", search_id)
    return item


def _unique_search_name(session: Session, name: str, exclude: UUID | None = None) -> None:
    existing = session.scalar(
        select(SavedView).where(
            SavedView.user_id == "local", SavedView.view_kind == "search", SavedView.name == name
        )
    )
    if existing is not None and existing.id != exclude:
        raise conflict("A saved search with this name already exists. Choose another name.")


@router.get("/saved-searches")
def list_saved_searches(request: Request, cursor: str | None = None) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        where = (
            SavedView.user_id == "local",
            SavedView.view_kind == "search",
            SavedView.deleted_at.is_(None),
        )
        total = session.scalar(select(func.count()).select_from(SavedView).where(*where)) or 0
        rows = session.scalars(
            select(SavedView)
            .where(*where)
            .order_by(SavedView.name, SavedView.id)
            .offset(offset)
            .limit(PAGE_SIZE)
        )
        return _list_payload([_saved_payload(item) for item in rows], offset, total)


@router.post("/saved-searches", status_code=201)
def create_saved_search(
    payload: SavedSearchCreate, request: Request, response: Response
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        if payload.definition.domain:
            _validate_domains(session, request, [payload.definition.domain])
        if payload.definition.collection_id:
            live_collection(session, payload.definition.collection_id)
        item = session.scalar(
            select(SavedView)
            .where(
                SavedView.user_id == "local",
                SavedView.view_kind == "search",
                SavedView.name == payload.name,
            )
            .with_for_update()
        )
        before = _saved_payload(item) if item else None
        if item is not None and item.deleted_at is None:
            raise conflict("A saved search with this name already exists. Choose another name.")
        if item is None:
            item = SavedView(user_id="local", view_kind="search", name=payload.name)
            session.add(item)
        else:
            item.deleted_at = None
        item.definition = payload.definition.model_dump(mode="json", exclude_none=True)
        session.flush()
        result = _saved_payload(item)
        audit(
            session,
            item,
            "saved_search",
            before,
            result,
            _request_id(request),
            action=AuditAction.RESTORE if before else AuditAction.CREATE,
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.get("/saved-searches/{search_id}")
def get_saved_search(search_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_search(session, search_id)
        response.headers["ETag"] = _etag(item.row_version)
        return _saved_payload(item)


@router.patch("/saved-searches/{search_id}")
def update_saved_search(
    search_id: UUID,
    payload: SavedSearchPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_search(session, search_id)
        _check_if_match(if_match, item.row_version, required=True)
        before = _saved_payload(item)
        if payload.name is not None:
            _unique_search_name(session, payload.name, item.id)
            item.name = payload.name
        if payload.definition is not None:
            if payload.definition.domain:
                _validate_domains(session, request, [payload.definition.domain])
            if payload.definition.collection_id:
                live_collection(session, payload.definition.collection_id)
            item.definition = payload.definition.model_dump(mode="json", exclude_none=True)
        session.flush()
        result = _saved_payload(item)
        audit(session, item, "saved_search", before, result, _request_id(request))
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.delete("/saved-searches/{search_id}", status_code=204)
def delete_saved_search(
    search_id: UUID, request: Request, if_match: str | None = Header(None, alias="If-Match")
) -> Response:
    with _v1._database(request).session() as session:
        item = _live_search(session, search_id)
        _check_if_match(if_match, item.row_version, required=True)
        before = _saved_payload(item)
        item.deleted_at = datetime.now(UTC)
        session.flush()
        audit(
            session,
            item,
            "saved_search",
            before,
            _saved_payload(item),
            _request_id(request),
            action=AuditAction.DELETE,
        )
    return Response(status_code=204)


def _position_payload(session: Session, position: ReadingPosition | None) -> dict[str, Any]:
    if position is None:
        return {"position": None, "stale": False}
    representation = session.get(Representation, position.representation_id)
    current_hash = representation.sha256 if representation is not None else None
    available = (
        representation is not None
        and representation.deleted_at is None
        and representation.availability not in {"missing", "blocked", "stale"}
        and representation_serving_status(session, representation).active
    )
    stale = not available or not current_hash or current_hash != position.source_hash
    return {
        "position": {
            "representation_id": str(position.representation_id),
            "page": position.page,
            "source_hash": position.source_hash,
            "row_version": position.row_version,
            "updated_at": position.updated_at.isoformat(),
        },
        "stale": stale,
        "current_source_hash": current_hash,
        "reason": "representation_unavailable"
        if not available
        else ("source_changed" if stale else None),
    }


@router.get("/assets/{asset_id}/reading-position")
def get_reading_position(asset_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        position = session.scalar(
            select(ReadingPosition).where(
                ReadingPosition.asset_id == asset.id, ReadingPosition.user_id == "local"
            )
        )
        if position is not None:
            response.headers["ETag"] = _etag(position.row_version)
        return _position_payload(session, position)


@router.put("/assets/{asset_id}/reading-position")
def save_reading_position(
    asset_id: UUID,
    payload: ReadingPositionUpdate,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        representation = session.get(Representation, payload.representation_id)
        if (
            representation is None
            or representation.asset_id != asset.id
            or representation.representation_type != "pdf"
            or representation.deleted_at is not None
            or representation.availability in {"missing", "blocked", "stale"}
            or not representation_serving_status(session, representation).active
        ):
            raise ApiProblem(422, "PDF 表示无效", "Choose an active PDF belonging to this asset.")
        if not representation.sha256:
            raise conflict(
                "The PDF has no registered content digest; "
                "verify it before saving a reading position."
            )
        if payload.source_hash and payload.source_hash.lower() != representation.sha256.lower():
            raise conflict(
                "The PDF digest changed. Reload the current representation before saving."
            )
        position = session.scalar(
            select(ReadingPosition)
            .where(ReadingPosition.asset_id == asset.id, ReadingPosition.user_id == "local")
            .with_for_update()
        )
        before = _position_payload(session, position) if position else None
        if position is None:
            position = ReadingPosition(
                asset_id=asset.id,
                representation_id=representation.id,
                page=payload.page,
                source_hash=representation.sha256,
            )
            session.add(position)
        else:
            _check_if_match(if_match, position.row_version, required=True)
            position.representation_id = representation.id
            position.page = payload.page
            position.source_hash = representation.sha256
        session.flush()
        result = _position_payload(session, position)
        audit(session, position, "reading_position", before, result, _request_id(request))
        response.headers["ETag"] = _etag(position.row_version)
        return result


@router.post("/exports", status_code=202)
def create_package_export(
    payload: PackageExportRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    values = payload.model_dump(mode="json")
    with _v1._database(request).session() as session:
        replay = replay_idempotent(
            session, scope="export-package", key=idempotency_key, request_payload=values
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        if payload.domain:
            _validate_domains(session, request, [payload.domain])
        job = Job(job_type="export_package", parameters=values, status="pending")
        session.add(job)
        session.flush()
        result = {"job_id": str(job.id), "status": "pending"}
        record_mutation(
            session,
            entity_type="job",
            entity_id=job.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="package-export.requested",
            idempotency_scope="export-package",
            idempotency_key=idempotency_key,
            request_payload=values,
            response_payload=result,
        )
        return result


@router.get("/exports/{job_id}/download")
def download_package_export(job_id: UUID, request: Request) -> FileResponse:
    with _v1._database(request).session() as session:
        job = session.get(Job, job_id)
        artifact = session.scalar(select(PackageExport).where(PackageExport.job_id == job_id))
        if job is None or job.job_type != "export_package":
            raise not_found("Export job", job_id)
        if job.status != "succeeded" or artifact is None:
            raise conflict(
                "The export artifact is not ready. "
                "Inspect the job status and retry after completion."
            )
        try:
            path = registered_package_path(_v1._settings(request), artifact.relative_path)
            from packages.ingestion.safe_tree_policy import sha256_file

            if not path.is_file() or sha256_file(path) != (artifact.sha256, artifact.size_bytes):
                raise PackageError("已登记归档产物缺失或摘要变化", 409)
        except (PackageError, OSError) as error:
            raise ApiProblem(
                409, "归档不可用", "The registered package is missing or its checksum changed."
            ) from error
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"research-domain-{job_id}.zip",
            headers={"ETag": f'"{artifact.sha256}"', "Cache-Control": "private, no-store"},
        )


@router.post("/imports/packages", status_code=201)
async def upload_package(
    request: Request,
    response: Response,
    filename: str = Query("domain-package.zip", min_length=1, max_length=255),
) -> dict[str, Any]:
    directory = package_directory(_v1._settings(request)) / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path_value = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=directory)
    path = Path(path_value)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            async for block in request.stream():
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise PackageError("领域包超过上传大小限制", 413)
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if not size:
            raise PackageError("请选择非空 ZIP 领域包")

        def complete_upload() -> tuple[dict[str, Any], int]:
            with _v1._database(request).session() as session:
                item = stage_package(
                    session,
                    _v1._settings(request),
                    path,
                    filename,
                    digest.hexdigest(),
                    _request_id(request),
                )
                return import_payload(item), item.row_version

        result, version = await run_in_threadpool(complete_upload)
        response.headers["ETag"] = _etag(version)
        return result
    except PackageError as error:
        raise ApiProblem(error.status, "领域包校验失败", str(error)) from error
    finally:
        await run_in_threadpool(path.unlink, missing_ok=True)


def _live_package_import(session: Session, import_id: UUID) -> PackageImport:
    item = session.get(PackageImport, import_id)
    if item is None:
        raise not_found("Package import", import_id)
    return item


@router.get("/imports/packages/{import_id}")
@router.get("/imports/packages/{import_id}/preview")
def get_package_import(import_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_package_import(session, import_id)
        response.headers["ETag"] = _etag(item.row_version)
        return import_payload(item)


@router.post("/imports/packages/{import_id}/commit")
def commit_package_import(
    import_id: UUID,
    request: Request,
    response: Response,
    payload: PackageCommitRequest | None = None,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        item = _live_package_import(session, import_id)
        session.refresh(item, with_for_update=True)
        if item.status != "committed":
            _check_if_match(if_match, item.row_version, required=True)
        try:
            result = commit_package(
                session,
                _v1._settings(request),
                item,
                payload or PackageCommitRequest(),
                _request_id(request),
            )
        except PackageError as error:
            raise ApiProblem(error.status, "领域包导入失败", str(error)) from error
        response.headers["ETag"] = _etag(item.row_version)
        return result
