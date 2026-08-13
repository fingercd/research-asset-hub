"""领域路由：资产、搜索、仪表盘与表示。"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Body, Header, Query, Request, Response
from sqlalchemy import func, select

from apps.api import routes_v1 as _v1
from apps.api.code_browser import CodeBrowserPathError, list_code_directory, read_code_file
from apps.api.contracts import AssetCreate, AssetPatch, ReadingStateUpdate
from apps.api.editor import CODE_REPRESENTATION_TYPES
from apps.api.events import record_mutation, replay_idempotent
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    RepresentationOpenRequest,
    _apply_asset_patch,
    _asset_context,
    _asset_facets,
    _asset_snapshot,
    _build_research_units,
    _check_if_match,
    _domains_with_counts,
    _editor_payload,
    _etag,
    _has_available_action,
    _is_system_hidden_asset,
    _list_payload,
    _live_asset,
    _local_code_context,
    _request_id,
    _resolve_asset_redirect,
    _serialize_representation_for_ui,
    _set_canonical_asset_headers,
    _tokens,
    router,
)
from apps.api.serialization import serialize_annotation, serialize_asset, serialize_representation
from apps.asset_gateway.security import create_gateway_token
from packages.domain.enums import AuditAction
from packages.domain.models import (
    Annotation,
    Asset,
    IndexState,
    ReadingState,
    Relation,
    Representation,
    Source,
)


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        assets = [
            asset
            for asset in session.scalars(
                select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.updated_at.desc())
            ).all()
            if not _is_system_hidden_asset(asset)
        ]
        states, summaries = _asset_context(session, assets)
        reading_counts = {"unread": 0, "reading": 0, "read": 0}
        for asset in assets:
            reading_state = states.get(asset.id)
            status = reading_state.status if reading_state is not None else "unread"
            reading_counts[status] = reading_counts.get(status, 0) + 1
        domains = _domains_with_counts(session, _v1._settings(request), assets)
        relation_count = (
            session.scalar(select(func.count(Relation.id)).where(Relation.deleted_at.is_(None)))
            or 0
        )
        annotation_count = (
            session.scalar(select(func.count(Annotation.id)).where(Annotation.deleted_at.is_(None)))
            or 0
        )
        recent = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
            )
            for item in assets[:8]
        ]
        continue_reading = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
            )
            for item in assets
            if states.get(item.id) is not None and states[item.id].status == "reading"
        ][:8]
        indexed_assets = (
            session.scalar(
                select(func.count(func.distinct(IndexState.entity_id))).where(
                    IndexState.entity_type == "asset",
                    IndexState.status == "ready",
                )
            )
            or 0
        )
        stats = {
            "assets": len(assets),
            "papers": sum(item.asset_type == "paper" for item in assets),
            **reading_counts,
            "domains": len(domains),
            "relations": relation_count,
            "annotations": annotation_count,
        }
        return {
            "stats": stats,
            "total_assets": stats["assets"],
            "total_papers": stats["papers"],
            "unread_count": stats["unread"],
            "reading_count": stats["reading"],
            "read_count": stats["read"],
            "domain_count": stats["domains"],
            "relation_count": stats["relations"],
            "annotation_count": stats["annotations"],
            "recent_assets": recent,
            "continue_reading": continue_reading,
            "domains": domains,
            "index_state": {
                "status": "ready" if indexed_assets else "building",
                "indexed_assets": indexed_assets,
                "pending_assets": max(0, len(assets) - indexed_assets),
            },
        }

@router.get("/search")
def search_assets(
    request: Request,
    q: str = "",
    mode: str = Query("keyword", pattern="^(exact|keyword)$"),
    group_by: str = Query("none", pattern="^(paper|none)$"),
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    author: str | None = None,
    asset_type: str | None = Query(None, alias="type"),
    year_from: int | None = Query(None, ge=1450, le=2200),
    year_to: int | None = Query(None, ge=1450, le=2200),
    tag: Annotated[list[str] | None, Query()] = None,
    has_pdf: bool | None = None,
    has_html: bool | None = None,
    has_code: bool | None = None,
    has_guide: bool | None = None,
    has_annotation: bool | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    paper_grouping = group_by == "paper" and asset_type in {None, "all", "paper"}
    filters: dict[str, Any] = {
        "domain": domain,
        "status": status,
        "type": asset_type if asset_type != "all" else None,
        "year_from": year_from,
        "year_to": year_to,
        "tags": tag,
        "has_pdf": has_pdf,
        "has_html": has_html,
        "has_code": has_code,
        "has_guide": has_guide,
    }
    filters = {key: value for key, value in filters.items() if value is not None}
    runtime_filters = {} if paper_grouping else filters
    search_hits: dict[UUID, Any] = {}
    search_detail: dict[str, Any] = {}
    if q.strip():
        try:
            search_window = max(100, offset + PAGE_SIZE)
            search_options: dict[str, Any] = {}
            if paper_grouping:
                with _v1._database(request).session() as count_session:
                    active_asset_count = (
                        count_session.scalar(
                            select(func.count(Asset.id)).where(Asset.deleted_at.is_(None))
                        )
                        or 0
                    )
                search_window = max(
                    search_window,
                    min(active_asset_count, 300),
                )
                search_options["candidate_limit"] = search_window
            result = _v1._search_runtime(request).service.search(
                q,
                mode=mode,
                filters=runtime_filters,
                limit=search_window,
                **search_options,
            )
            for hit in result.hits:
                try:
                    asset_id = UUID(hit.document.asset_id)
                except ValueError:
                    continue
                search_hits.setdefault(asset_id, hit)
            search_detail = {
                "degraded": result.degraded,
                "errors": list(result.errors),
                "mode": result.mode.value,
            }
        except Exception as error:
            search_detail = {
                "degraded": True,
                "errors": [f"keyword-runtime:{type(error).__name__}"],
                "detail": "PostgreSQL metadata fallback is active.",
            }

    with _v1._database(request).session() as session:
        catalog_assets = [
            asset
            for asset in session.scalars(
                select(Asset)
                .where(Asset.deleted_at.is_(None))
                .order_by(Asset.updated_at.desc(), Asset.id)
            ).all()
            if not _is_system_hidden_asset(asset)
        ]
        if search_hits:
            indexed_assets = {item.id: item for item in catalog_assets if item.id in search_hits}
            assets = [
                indexed_assets[asset_id] for asset_id in search_hits if asset_id in indexed_assets
            ]
            if not assets:
                search_hits.clear()
                search_detail = {
                    "degraded": True,
                    "errors": ["keyword-runtime:stale-or-foreign-index"],
                    "detail": "PostgreSQL metadata fallback is active.",
                }
                assets = catalog_assets
        else:
            assets = catalog_assets

        context_assets = catalog_assets if paper_grouping else assets
        states, summaries = _asset_context(session, context_assets)
        context_asset_ids = [asset.id for asset in context_assets]
        annotation_assets = {
            asset_id
            for asset_id in session.scalars(
                select(Annotation.asset_id).where(
                    Annotation.deleted_at.is_(None),
                    Annotation.asset_id.in_(context_asset_ids),
                )
            )
        }
        representations: dict[UUID, list[Representation]] = defaultdict(list)
        if context_asset_ids:
            for representation in session.scalars(
                select(Representation)
                .where(
                    Representation.deleted_at.is_(None),
                    Representation.asset_id.in_(context_asset_ids),
                )
                .order_by(
                    Representation.asset_id,
                    Representation.role,
                    Representation.representation_type,
                    Representation.id,
                )
            ):
                representations[representation.asset_id].append(representation)
        filtered: list[Asset] = []
        for asset in assets:
            if not paper_grouping:
                if domain and domain not in (asset.domain_slugs or []):
                    continue
                if asset_type and asset_type != "all" and asset.asset_type != asset_type:
                    continue
                if author and not any(
                    author.casefold() in item.casefold() for item in asset.authors or []
                ):
                    continue
                if year_from is not None and (asset.year is None or asset.year < year_from):
                    continue
                if year_to is not None and (asset.year is None or asset.year > year_to):
                    continue
                if tag and not set(tag).intersection(asset.tags or []):
                    continue
                reading_state = states.get(asset.id)
                reading_status = reading_state.status if reading_state is not None else "unread"
                if status and reading_status != status:
                    continue
                if has_annotation is not None and (
                    (asset.id in annotation_assets) != has_annotation
                ):
                    continue
                asset_representations = representations.get(asset.id, [])
                availability_flags = {
                    kind: _has_available_action(asset_representations, kind)
                    for kind in ("pdf", "html", "code", "guide")
                }
                if has_pdf is not None and availability_flags["pdf"] != has_pdf:
                    continue
                if has_html is not None and availability_flags["html"] != has_html:
                    continue
                if has_code is not None and availability_flags["code"] != has_code:
                    continue
                if has_guide is not None and availability_flags["guide"] != has_guide:
                    continue
            if q.strip() and not search_hits:
                haystack = " ".join(
                    [
                        asset.title or "",
                        asset.title_zh or "",
                        " ".join(asset.authors or []),
                        asset.abstract or "",
                        asset.summary or "",
                        " ".join(asset.tags or []),
                    ]
                ).casefold()
                if q.casefold() not in haystack and not any(
                    token in haystack for token in _tokens(q)
                ):
                    continue
            filtered.append(asset)

        if paper_grouping:
            units, facet_assets = _build_research_units(
                session,
                catalog_assets=catalog_assets,
                matched_assets=filtered,
                search_hits=search_hits,
                query=q,
                states=states,
                summaries=summaries,
                annotation_assets=annotation_assets,
                representations=representations,
                include_independent_repositories=asset_type != "paper",
                domain=domain,
                status=status,
                author=author,
                year_from=year_from,
                year_to=year_to,
                tags=tag,
                has_pdf=has_pdf,
                has_html=has_html,
                has_code=has_code,
                has_guide=has_guide,
                has_annotation=has_annotation,
                source_registry=_v1._source_registry(request),
            )
            payload = _list_payload(units[offset : offset + PAGE_SIZE], offset, len(units))
            payload["mode"] = mode
            payload["group_by"] = "paper"
            payload.update(search_detail)
            payload.setdefault("degraded", bool(q.strip() and not search_hits))
            payload["facets"] = _asset_facets(facet_assets, states)
            return payload

        page = filtered[offset : offset + PAGE_SIZE]
        items: list[dict[str, Any]] = []
        for asset in page:
            selected_hit = search_hits.get(asset.id)
            item = serialize_asset(
                asset,
                reading_state=states.get(asset.id),
                annotation_summary=summaries.get(asset.id),
                score=(round(float(selected_hit.score), 6) if selected_hit is not None else 1.0),
            )
            if selected_hit is not None:
                item.update(
                    {
                        "snippet": " ".join(selected_hit.document.text.split())[:360],
                        "document_kind": selected_hit.document.kind,
                        "anchor": selected_hit.document.metadata.get("anchor"),
                        "representation_id": selected_hit.document.metadata.get(
                            "representation_id"
                        ),
                        "search_source": selected_hit.source,
                        "exact_match": selected_hit.exact_match,
                    }
                )
            item["representations"] = [
                serialize_representation(representation)
                for representation in representations.get(asset.id, [])
                if representation.availability == "available"
            ]
            items.append(item)
        payload = _list_payload(items, offset, len(filtered))
        payload["mode"] = mode
        payload.update(search_detail)
        payload.setdefault("degraded", bool(q.strip() and not search_hits))
        payload["facets"] = _asset_facets(filtered, states)
        return payload

@router.get("/assets")
def list_assets(
    request: Request,
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    cursor: str | None = None,
    include_deleted: bool = False,
    include: str | None = Query(None, pattern="^representations$"),
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _v1._database(request).session() as session:
        statement = select(Asset).order_by(Asset.updated_at.desc(), Asset.id)
        if not include_deleted:
            statement = statement.where(Asset.deleted_at.is_(None))
        assets = [
            asset
            for asset in session.scalars(statement).all()
            if not _is_system_hidden_asset(asset)
        ]
        states, summaries = _asset_context(session, assets)
        filtered: list[Asset] = []
        for asset in assets:
            if domain and domain not in (asset.domain_slugs or []):
                continue
            reading_state = states.get(asset.id)
            current_status = reading_state.status if reading_state is not None else "unread"
            if status and current_status != status:
                continue
            filtered.append(asset)
        page = filtered[offset : offset + PAGE_SIZE]
        items = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
            )
            for item in page
        ]
        if include == "representations":
            page_representations: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
            if page:
                for representation in session.scalars(
                    select(Representation)
                    .where(
                        Representation.asset_id.in_([item.id for item in page]),
                        Representation.deleted_at.is_(None),
                    )
                    .order_by(
                        Representation.asset_id,
                        Representation.role,
                        Representation.representation_type,
                        Representation.id,
                    )
                ):
                    page_representations[representation.asset_id].append(
                        serialize_representation(representation)
                    )
            for item, asset in zip(items, page, strict=True):
                item["representations"] = page_representations[asset.id]
        return _list_payload(items, offset, len(filtered))

@router.post("/assets", status_code=201)
def create_asset(
    payload: AssetCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _v1._database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="create-asset",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        asset = Asset(**payload.model_dump())
        session.add(asset)
        session.flush()
        result = serialize_asset(asset)
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="asset.created",
            idempotency_scope="create-asset",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(asset.row_version)
        return result

@router.get("/assets/{asset_id}")
def get_asset(asset_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        state = session.scalar(
            select(ReadingState).where(
                ReadingState.asset_id == asset.id,
                ReadingState.user_id == "local",
            )
        )
        annotation = session.scalar(
            select(Annotation)
            .where(
                Annotation.asset_id == asset.id,
                Annotation.deleted_at.is_(None),
            )
            .order_by(Annotation.pinned.desc(), Annotation.updated_at.desc())
        )
        summary = None
        if annotation is not None:
            summary = " ".join((annotation.summary or annotation.body_markdown or "").split())[:80]
        result = serialize_asset(asset, reading_state=state, annotation_summary=summary)
        result["representations"] = [
            serialize_representation(item)
            for item in session.scalars(
                select(Representation).where(
                    Representation.asset_id == asset.id,
                    Representation.deleted_at.is_(None),
                )
            ).all()
        ]
        result["annotations"] = [
            serialize_annotation(item)
            for item in session.scalars(
                select(Annotation).where(
                    Annotation.asset_id == asset.id,
                    Annotation.deleted_at.is_(None),
                )
            ).all()
        ]
        response.headers["ETag"] = _etag(asset.row_version)
        return result

@router.get("/asset-redirects/{from_asset_id}")
@router.get("/assets/{from_asset_id}/redirect")
def get_asset_redirect(
    from_asset_id: UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        canonical_id, chain = _resolve_asset_redirect(session, from_asset_id)
        if not chain:
            raise not_found("AssetRedirect", from_asset_id)
        canonical = session.get(Asset, canonical_id)
        if canonical is None:
            raise ApiProblem(
                500,
                "资产重定向损坏",
                f"The redirect target {canonical_id} does not exist.",
                problem_type="urn:research-asset-hub:problem:broken-redirect",
            )
        _set_canonical_asset_headers(
            response,
            requested_id=from_asset_id,
            canonical_id=canonical_id,
        )
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return {
            "from_asset_id": str(from_asset_id),
            "to_asset_id": str(chain[0].to_asset_id),
            "canonical_asset_id": str(canonical_id),
            "permanent": True,
            "canonical_deleted": canonical.deleted_at is not None,
            "chain": [
                {
                    "from_asset_id": str(item.from_asset_id),
                    "to_asset_id": str(item.to_asset_id),
                    "reason": item.reason,
                    "created_at": item.created_at.isoformat(),
                }
                for item in chain
            ],
        }

@router.patch("/assets/{asset_id}")
def update_asset(
    asset_id: UUID,
    payload: AssetPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        _check_if_match(if_match, asset.row_version, required=True)
        before = _asset_snapshot(asset)
        _apply_asset_patch(asset, payload)
        session.flush()
        result = serialize_asset(asset)
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="asset.updated",
        )
        response.headers["ETag"] = _etag(asset.row_version)
        return result

@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(
    asset_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _check_if_match(if_match, asset.row_version)
        before = _asset_snapshot(asset)
        asset.deleted_at = datetime.now(UTC)
        session.flush()
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.DELETE,
            before=before,
            after=_asset_snapshot(asset),
            request_id=_request_id(request),
            event_type="asset.deleted",
        )
    return Response(status_code=204)

@router.post("/assets/{asset_id}/restore")
def restore_asset(
    asset_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id, include_deleted=True)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        _check_if_match(if_match, asset.row_version)
        if asset.deleted_at is None:
            result = serialize_asset(asset)
            response.headers["ETag"] = _etag(asset.row_version)
            return result
        before = _asset_snapshot(asset)
        asset.deleted_at = None
        session.flush()
        result = serialize_asset(asset)
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="asset.restored",
        )
        response.headers["ETag"] = _etag(asset.row_version)
        return result

@router.put("/assets/{asset_id}/reading-state")
def update_reading_state(
    asset_id: UUID,
    payload: ReadingStateUpdate,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    now = datetime.now(UTC)
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        state = session.scalar(
            select(ReadingState).where(
                ReadingState.asset_id == asset.id,
                ReadingState.user_id == "local",
            )
        )
        before = (
            {"status": state.status, "row_version": state.row_version}
            if state is not None
            else None
        )
        if state is None:
            state = ReadingState(user_id="local", asset_id=asset.id)
            session.add(state)
            session.flush()
        _check_if_match(if_match, state.row_version)
        state.status = payload.status
        if payload.status == "reading" and state.started_at is None:
            state.started_at = now
        if payload.status == "read":
            state.started_at = state.started_at or now
            state.read_at = now
        elif payload.status == "unread":
            state.started_at = None
            state.read_at = None
        session.flush()
        after = {"status": state.status, "row_version": state.row_version}
        record_mutation(
            session,
            entity_type="reading_state",
            entity_id=state.id,
            action=AuditAction.UPDATE if before is not None else AuditAction.CREATE,
            before=before,
            after=after,
            request_id=_request_id(request),
            event_type="reading-state.updated",
        )
        response.headers["ETag"] = _etag(state.row_version)
        return serialize_asset(asset, reading_state=state)

@router.get("/assets/{asset_id}/representations")
def list_representations(asset_id: UUID, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        items = list(
            session.scalars(
                select(Representation)
                .where(
                    Representation.asset_id == asset.id,
                    Representation.deleted_at.is_(None),
                )
                .order_by(Representation.role, Representation.representation_type)
            ).all()
        )
        source_aliases = {
            source.id: source.alias
            for source in session.scalars(
                select(Source).where(
                    Source.id.in_(
                        {
                            item.source_id
                            for item in items
                            if item.source_id is not None
                        }
                    )
                )
            ).all()
        }
        return {
            "items": [
                _serialize_representation_for_ui(
                    request,
                    item,
                    source_alias=source_aliases.get(item.source_id),
                )
                for item in items
            ],
            "next_cursor": None,
            "total": len(items),
        }

@router.get("/representations/{representation_id}/code/tree")
def code_tree(
    representation_id: UUID,
    request: Request,
    path: str = Query(default=""),
) -> dict[str, Any]:
    representation, root = _local_code_context(request, representation_id)
    try:
        return list_code_directory(
            root,
            representation_id=str(representation.id),
            relative_path=path,
        )
    except CodeBrowserPathError as error:
        raise conflict(str(error)) from error

@router.get("/representations/{representation_id}/code/file")
def code_file(
    representation_id: UUID,
    request: Request,
    path: str = Query(..., min_length=1),
) -> dict[str, Any]:
    representation, root = _local_code_context(request, representation_id)
    try:
        return read_code_file(
            root,
            representation_id=str(representation.id),
            relative_path=path,
        )
    except CodeBrowserPathError as error:
        raise conflict(str(error)) from error

@router.post("/representations/{representation_id}/open-actions")
def open_representation(
    representation_id: UUID,
    request: Request,
    payload: Annotated[RepresentationOpenRequest | None, Body()] = None,
) -> dict[str, Any]:
    settings = _v1._settings(request)
    requested_presentation = payload.presentation if payload is not None else "original"
    embed = payload.embed if payload is not None else False
    with _v1._database(request).session() as session:
        representation = session.get(Representation, representation_id)
        if representation is None or representation.deleted_at is not None:
            raise not_found("Representation", representation_id)
        if representation.availability != "available":
            raise conflict("Only representations with availability=available can be opened.")
        editor_payload: dict[str, Any] | None = None
        if representation.representation_type in CODE_REPRESENTATION_TYPES:
            source = (
                session.get(Source, representation.source_id)
                if representation.source_id is not None
                else None
            )
            editor_payload = _editor_payload(
                request,
                representation,
                source_alias=source.alias if source is not None else None,
            )
        if representation.external_url and not representation.path_ref:
            result: dict[str, Any] = {
                "kind": "external_url",
                "url": representation.external_url,
                "network_required": representation.network_required,
                "presentation": "original",
                "embed": embed,
            }
            if editor_payload is not None:
                result["editor"] = editor_payload
            if requested_presentation == "hub":
                result["warning"] = (
                    "Hub presentation is unavailable for an external-only representation."
                )
            return result
        try:
            token = create_gateway_token(
                settings.gateway_secret,
                representation.id,
                ttl_seconds=settings.gateway_session_seconds,
                nonce=uuid4().hex,
            )
        except ValueError as error:
            raise ApiProblem(
                503,
                "资产网关未配置",
                str(error),
                problem_type="urn:research-asset-hub:problem:gateway-configuration",
            ) from error
        origin = settings.gateway_origin.rstrip("/")
        url = f"{origin}/session/{representation.id}?token={quote(token, safe='')}"
        if payload is not None:
            url = f"{url}&presentation={requested_presentation}"
        result = {
            "kind": "gateway_session",
            "url": url,
            "expires_in": settings.gateway_session_seconds,
            "representation_id": str(representation.id),
            "presentation": requested_presentation,
            "embed": embed,
        }
        if editor_payload is not None:
            result["editor"] = editor_payload
        return result
