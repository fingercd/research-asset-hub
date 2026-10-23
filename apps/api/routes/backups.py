"""领域路由：备份。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Body, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from apps.api import routes_v1 as _v1
from apps.api.config import PROJECT_ROOT
from apps.api.events import record_mutation
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _etag,
    _list_payload,
    _request_id,
    _serialize_backup_run,
    router,
)
from packages.backup import BackupManager, create_platform_backup, default_backup_repository
from packages.domain.enums import AuditAction, BackupStatus
from packages.domain.models import BackupRun, Representation, Source
from packages.domain.serving_scope import active_representation_clause


class BackupOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_external_files: bool = False


@router.get("/backups")
def list_backups(
    request: Request,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        runs = list(session.scalars(select(BackupRun).order_by(BackupRun.created_at.desc())).all())
        page = runs[offset : offset + PAGE_SIZE]
        return _list_payload(
            [_serialize_backup_run(item) for item in page],
            offset,
            len(runs),
        )


@router.post("/backups", status_code=201)
def create_backup(
    request: Request,
    options: Annotated[BackupOptions | None, Body()] = None,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    database = _v1._database(request)
    with database.session() as session:
        if idempotency_key:
            existing = next(
                (
                    item
                    for item in session.scalars(
                        select(BackupRun).order_by(BackupRun.created_at.desc())
                    )
                    if (item.counters or {}).get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing is not None:
                return _serialize_backup_run(existing)
        run = BackupRun(
            status=BackupStatus.RUNNING.value,
            backup_kind="full",
            destination_label=str(default_backup_repository()),
            local_copy_only=True,
            counters={"idempotency_key": idempotency_key},
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        run_id = run.id
        record_mutation(
            session,
            entity_type="backup_run",
            entity_id=run.id,
            action=AuditAction.CREATE,
            before=None,
            after=_serialize_backup_run(run),
            request_id=_request_id(request),
            event_type="backup.started",
        )

    try:
        settings = _v1._settings(request)
        external_inputs: dict[str, Path] = {}
        if options and options.include_external_files:
            registry = _v1._source_registry(request)
            managed_library = (settings.data_dir / "library").resolve()
            with database.session() as session:
                rows = session.execute(
                    select(Representation, Source)
                    .join(Source, Source.id == Representation.source_id)
                    .where(
                        Representation.deleted_at.is_(None),
                        Representation.availability == "available",
                        Source.enabled.is_(True),
                        active_representation_clause(),
                    )
                )
                for representation, source in rows:
                    if not representation.path_ref:
                        continue
                    path = registry.resolve(
                        source.alias, representation.root_key, representation.path_ref
                    )
                    if not path.is_relative_to(managed_library):
                        external_inputs[str(representation.id)] = path
        result = create_platform_backup(
            PROJECT_ROOT, data_dir=settings.data_dir, external_inputs=external_inputs
        )
    except Exception as error:
        with database.session() as session:
            failed = session.get(BackupRun, run_id, with_for_update=True)
            if failed is not None:
                failed.status = BackupStatus.FAILED.value
                failed.error = {
                    "type": type(error).__name__,
                    "detail": " ".join(str(error).split())[:500],
                }
                failed.finished_at = datetime.now(UTC)
        raise ApiProblem(
            503,
            "备份失败",
            "The local encrypted backup could not be completed; inspect the backup run.",
            problem_type="urn:research-asset-hub:problem:backup-failed",
        ) from error

    with database.session() as session:
        completed = session.get(BackupRun, run_id, with_for_update=True)
        if completed is None:
            raise RuntimeError("Backup run disappeared after Restic completed.")
        before = _serialize_backup_run(completed)
        completed.status = BackupStatus.VERIFIED.value
        completed.manifest_path = str(result.get("staging_manifest") or "")
        completed.counters = {
            **dict(completed.counters or {}),
            **result,
        }
        completed.finished_at = datetime.now(UTC)
        completed.verified_at = datetime.now(UTC)
        manifest_path = Path(completed.manifest_path)
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tree_digests = manifest.get("tree_digests")
            if isinstance(tree_digests, dict):
                completed.export_digest = tree_digests.get("open_catalog")
            files = manifest.get("files")
            if isinstance(files, dict):
                dump = next(
                    (
                        values
                        for path, values in files.items()
                        if str(path).endswith("/postgres.dump")
                    ),
                    None,
                )
                if isinstance(dump, dict):
                    completed.database_digest = str(dump.get("sha256") or "") or None
        session.flush()
        response = _serialize_backup_run(completed)
        record_mutation(
            session,
            entity_type="backup_run",
            entity_id=completed.id,
            action=AuditAction.UPDATE,
            before=before,
            after=response,
            request_id=_request_id(request),
            event_type="backup.verified",
        )
        return response


@router.get("/backups/restore-rehearsal/latest")
def latest_restore_rehearsal() -> dict[str, Any]:
    root = PROJECT_ROOT / "work" / "restore-tests"
    reports = sorted(
        root.glob("*/report.json") if root.is_dir() else (),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not reports:
        return {
            "status": "not_run",
            "risk": "local-copy-only",
            "detail": "No isolated restore rehearsal report exists yet.",
        }
    report_path = reports[0]
    raw_report = json.loads(report_path.read_text(encoding="utf-8"))
    report: dict[str, Any] = (
        dict(raw_report) if isinstance(raw_report, dict) else {"status": "invalid_report"}
    )
    report["report_path"] = str(report_path)
    return report


@router.post("/backups/{backup_id}/verify")
def verify_backup(
    backup_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    database = _v1._database(request)
    with database.session() as session:
        run = session.get(BackupRun, backup_id)
        if run is None:
            raise not_found("Backup run", backup_id)
        _check_if_match(if_match, run.row_version)
        counters = dict(run.counters or {})
        snapshot_id = counters.get("snapshot_id")
        if (
            run.status not in {BackupStatus.SUCCEEDED.value, BackupStatus.VERIFIED.value}
            or run.error
            or not snapshot_id
        ):
            raise conflict("Only a successful backup with a recorded snapshot can be verified.")
        repository = counters.get("repository") or run.destination_label
        version = run.row_version

    try:
        verification = BackupManager(repository=Path(repository)).verify_snapshot(snapshot_id)
    except (OSError, RuntimeError, ValueError) as error:
        raise ApiProblem(
            503,
            "备份验证失败",
            "The recorded snapshot could not be verified; the backup status is unchanged.",
            problem_type="urn:research-asset-hub:problem:backup-verification-failed",
        ) from error

    with database.session() as session:
        run = session.get(BackupRun, backup_id, with_for_update=True)
        if run is None:
            raise not_found("Backup run", backup_id)
        _check_if_match(_etag(version), run.row_version)
        before = _serialize_backup_run(run)
        run.status = BackupStatus.VERIFIED.value
        run.verified_at = datetime.now(UTC)
        run.counters = {
            **dict(run.counters or {}),
            "snapshot_id": verification["snapshot_id"],
            "repository_check": verification["repository_check"],
            "snapshot_verification": verification,
        }
        session.flush()
        result = _serialize_backup_run(run)
        record_mutation(
            session,
            entity_type="backup_run",
            entity_id=run.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="backup.snapshot-verified",
        )
        response.headers["ETag"] = _etag(run.row_version)
        return result
