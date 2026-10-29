"""领域路由：第三方集成与同步冲突。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import Header, Request, Response
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import routes_v1 as _v1
from apps.api.contracts import AssetPatch, ConflictResolutionRequest, SyncApplyRequest
from apps.api.events import record_mutation
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    _check_if_match,
    _etag,
    _list_payload,
    _request_id,
    _sync_etag,
    router,
)
from apps.api.serialization import serialize_sync_conflict
from packages.domain.enums import (
    AuditAction,
    ConflictResolution,
    IntegrationStatus,
    SyncConflictStatus,
    SyncStatus,
)
from packages.domain.models import Integration, SyncConflict, SyncRun
from packages.integrations import (
    ZoteroLocalClient,
    obsidian_sync_etag,
    preview_obsidian_assets,
    sync_obsidian_assets,
)
from packages.integrations.obsidian import ObsidianContentChanged
from packages.integrations.obsidian_sync import apply_obsidian_conflict


@router.get("/sync-conflicts")
def list_sync_conflicts(
    request: Request,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        statement = select(SyncConflict).order_by(SyncConflict.created_at.desc())
        if status:
            statement = statement.where(SyncConflict.status == status)
        conflicts = list(session.scalars(statement).all())
        page = conflicts[offset : offset + PAGE_SIZE]
        return _list_payload(
            [serialize_sync_conflict(item) for item in page],
            offset,
            len(conflicts),
        )


@router.post("/integrations/{provider}/sync/preview")
def preview_integration_sync(
    provider: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    provider = provider.casefold()
    settings = _v1._settings(request)
    if provider == "obsidian":
        with _v1._database(request).session() as session:
            result = preview_obsidian_assets(
                session,
                settings.obsidian_vault_path,
                hub_directory=settings.obsidian_hub_directory,
            )
            response.headers["ETag"] = f'"{result["etag"]}"'
            return result
    if provider != "zotero":
        raise ApiProblem(
            404,
            "未知集成",
            f"The integration provider {provider!r} is not supported.",
            problem_type="urn:research-asset-hub:problem:integration-not-found",
        )

    client = ZoteroLocalClient(settings.zotero_base_url)
    api_ready = client.api_ready()
    items: list[dict[str, Any]] = []
    if api_ready:
        try:
            items = client.list_items()
        except (httpx.HTTPError, ValueError, OSError):
            api_ready = False
    bbt = client.better_bibtex_ready() if api_ready else None
    package_paths = [
        settings.zotero_export_path / "research-assets.csl.json",
        settings.zotero_export_path / "research-assets.bib",
    ]
    package_state = [
        {
            "path": str(path),
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "mtime_ns": path.stat().st_mtime_ns if path.exists() else 0,
        }
        for path in package_paths
    ]
    etag = _sync_etag(
        {
            "api_ready": api_ready,
            "item_count": len(items),
            "bbt": bbt,
            "packages": package_state,
        }
    )
    with _v1._database(request).session() as session:
        integration = session.scalar(
            select(Integration).where(
                Integration.provider == "zotero",
                Integration.name == "Local Zotero",
            )
        )
        if integration is None:
            integration = Integration(
                provider="zotero",
                name="Local Zotero",
                config={"base_url": settings.zotero_base_url},
                enabled=True,
                status=(
                    IntegrationStatus.READY.value if api_ready else IntegrationStatus.ERROR.value
                ),
            )
            session.add(integration)
            session.flush()
        else:
            integration.status = (
                IntegrationStatus.READY.value if api_ready else IntegrationStatus.ERROR.value
            )
            integration.error = None if api_ready else {"code": "zotero_not_running"}
        run = SyncRun(
            integration_id=integration.id,
            status=SyncStatus.SUCCEEDED.value,
            mode="read-and-export-preview",
            preview_only=True,
            counters={
                "etag": etag,
                "zotero_items": len(items),
                "packages": package_state,
                "api_ready": api_ready,
                "better_bibtex_ready": bbt is not None,
            },
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        result = {
            "status": run.status,
            "sync_run_id": str(run.id),
            "preview_only": True,
            "etag": etag,
            "api_ready": api_ready,
            "better_bibtex": bbt,
            "zotero_items": len(items),
            "packages": package_state,
            "apply_mode": "reviewed-manual-import",
        }
        response.headers["ETag"] = f'"{etag}"'
        return result


@router.post("/integrations/{provider}/sync/apply")
def apply_integration_sync(
    provider: str,
    payload: SyncApplyRequest,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    provider = provider.casefold()
    settings = _v1._settings(request)
    with _v1._database(request).session() as session:
        preview = session.get(SyncRun, payload.preview_id)
        if preview is None or not preview.preview_only:
            raise not_found("Sync preview", payload.preview_id)
        integration = session.get(Integration, preview.integration_id)
        if integration is None or integration.provider != provider:
            raise not_found("Integration preview", payload.preview_id)
        expected = str((preview.counters or {}).get("etag") or "")
        supplied = (if_match or "").strip().strip('"')
        if not supplied or supplied != expected:
            raise ApiProblem(
                412,
                "同步预览已失效",
                "Apply requires the exact ETag returned by the selected preview.",
                problem_type="urn:research-asset-hub:problem:sync-preview-stale",
                headers={"ETag": f'"{expected}"'} if expected else None,
            )

        if provider == "obsidian":
            current = obsidian_sync_etag(
                session,
                settings.obsidian_vault_path,
                hub_directory=settings.obsidian_hub_directory,
            )
            if current != expected:
                raise ApiProblem(
                    412,
                    "同步预览已失效",
                    "Hub or Obsidian content changed after preview; create a new preview.",
                    problem_type="urn:research-asset-hub:problem:sync-preview-stale",
                    headers={"ETag": f'"{current}"'},
                )
            try:
                result = sync_obsidian_assets(
                    session,
                    settings.obsidian_vault_path,
                    hub_directory=settings.obsidian_hub_directory,
                    expected_etag=expected,
                )
            except ObsidianContentChanged as exc:
                raise ApiProblem(
                    412,
                    "同步预览已失效",
                    str(exc),
                    problem_type="urn:research-asset-hub:problem:sync-preview-stale",
                ) from exc
            response.headers["ETag"] = f'"{current}"'
            return result

        if provider == "zotero":
            packages = [
                settings.zotero_export_path / "research-assets.csl.json",
                settings.zotero_export_path / "research-assets.bib",
            ]
            if not all(path.is_file() for path in packages):
                raise ApiProblem(
                    409,
                    "Zotero 导入包缺失",
                    "Generate the CSL JSON and BibTeX packages before applying this preview.",
                    problem_type="urn:research-asset-hub:problem:zotero-package-missing",
                )
            run = SyncRun(
                integration_id=integration.id,
                status=SyncStatus.NEEDS_REVIEW.value,
                mode="manual-import-package",
                preview_only=False,
                counters={
                    "packages": [str(path) for path in packages],
                    "external_write": False,
                    "requires_user_confirmation": True,
                },
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            session.add(run)
            session.flush()
            return {
                "status": run.status,
                "sync_run_id": str(run.id),
                "external_write": False,
                "requires_user_confirmation": True,
                "packages": [str(path) for path in packages],
                "detail": (
                    "Zotero local API is read-only; review and import one of these packages, "
                    "then run preview again to read back the resulting library."
                ),
            }

        raise not_found("Integration", provider)


@router.post("/sync-conflicts/{conflict_id}/resolve")
def resolve_sync_conflict(
    conflict_id: UUID,
    payload: ConflictResolutionRequest,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    mapping = {
        "keep_local": (ConflictResolution.HUB.value, "hub_value"),
        "accept_incoming": (ConflictResolution.EXTERNAL.value, "external_value"),
        "merge": (ConflictResolution.MANUAL.value, "manual"),
    }
    with _v1._database(request).session() as session:
        item = session.get(SyncConflict, conflict_id)
        if item is None:
            raise not_found("Sync conflict", conflict_id)
        _check_if_match(if_match, item.row_version)
        if item.status != SyncConflictStatus.PENDING.value:
            raise conflict("This sync conflict has already been resolved.")
        resolution, source = mapping[payload.strategy]
        if source == "hub_value":
            resolved_value = item.hub_value
        elif source == "external_value":
            resolved_value = item.external_value
        else:
            if "resolved_value" not in payload.model_fields_set:
                raise ApiProblem(
                    422,
                    "合并值缺失",
                    "The merge strategy requires resolved_value.",
                    problem_type="urn:research-asset-hub:problem:validation",
                )
            resolved_value = payload.resolved_value
        before = serialize_sync_conflict(item)
        field = item.field_path.removeprefix("/")
        model_field = {"domains": "domain_slugs"}.get(field, field)
        try:
            if model_field in AssetPatch.model_fields:
                validated = AssetPatch.model_validate({model_field: resolved_value})
                resolved_value = getattr(validated, model_field)
            apply_obsidian_conflict(session, item, resolved_value)
        except ObsidianContentChanged as exc:
            raise conflict(str(exc)) from exc
        except (ValidationError, ValueError) as exc:
            raise ApiProblem(
                422,
                "合并值无效",
                str(exc),
                problem_type="urn:research-asset-hub:problem:validation",
            ) from exc
        item.status = SyncConflictStatus.RESOLVED.value
        item.resolution = resolution
        item.resolved_value = resolved_value
        item.resolved_by = "local"
        item.resolved_at = datetime.now(UTC)
        session.flush()
        result = serialize_sync_conflict(item)
        record_mutation(
            session,
            entity_type="sync_conflict",
            entity_id=item.id,
            action=AuditAction.MERGE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="sync-conflict.resolved",
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result
