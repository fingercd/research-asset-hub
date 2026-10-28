"""领域路由：资产、搜索、仪表盘与表示。"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Body, Header, Query, Request, Response
from sqlalchemy import case, func, select

from apps.api import routes_v1 as _v1
from apps.api.catalog_queries import (
    catalog_facets,
    catalog_selection,
    confirmed_links,
    domain_counts,
    domain_membership,
    hidden_clause,
    load_catalog_assets,
    metadata_match,
    reading_status,
    serialize_catalog_asset,
    sorted_selection,
    visible_clause,
)
from apps.api.code_browser import CodeBrowserPathError, list_code_directory, read_code_file
from apps.api.contracts import AssetCreate, AssetPatch, ReadingStateUpdate
from apps.api.domain_views import annotate_presentation_domains
from apps.api.editor import CODE_REPRESENTATION_TYPES
from apps.api.events import record_mutation, replay_idempotent
from apps.api.pagination import decode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.routes_v1 import (
    PAGE_SIZE,
    RepresentationOpenRequest,
    _apply_asset_patch,
    _asset_context,
    _asset_snapshot,
    _build_research_units,
    _check_if_match,
    _editor_payload,
    _etag,
    _list_payload,
    _live_asset,
    _local_code_context,
    _request_id,
    _research_unit_quick_actions,
    _resolve_asset_redirect,
    _retired_representation_problem,
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
from packages.domain.serving_scope import (
    RetiredRepresentationError,
    active_representation_clause,
    active_representation_ids,
    assert_representation_active,
    representation_serving_status,
    retired_source_ids,
)
from packages.domain.workflow_models import ReadingPosition
from packages.workflows.service import live_collection


def _uuid_metadata(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _filter_search_hits_for_serving(
    session: Any,
    candidates: dict[UUID, list[Any]],
) -> dict[UUID, Any]:
    """Pick the first hit per asset that is not backed by retired provenance."""

    representation_ids = {
        representation_id
        for hits in candidates.values()
        for hit in hits
        if (representation_id := _uuid_metadata(hit.document.metadata.get("representation_id")))
        is not None
    }
    source_ids = {
        source_id
        for hits in candidates.values()
        for hit in hits
        if (source_id := _uuid_metadata(hit.document.metadata.get("source_id"))) is not None
    }
    active_representations = active_representation_ids(session, representation_ids)
    retired_sources = retired_source_ids(session, source_ids)
    result: dict[UUID, Any] = {}
    for asset_id, hits in candidates.items():
        for hit in hits:
            raw_representation_id = hit.document.metadata.get("representation_id")
            representation_id = _uuid_metadata(raw_representation_id)
            if raw_representation_id and (
                representation_id is None or representation_id not in active_representations
            ):
                continue
            raw_source_id = hit.document.metadata.get("source_id")
            source_id = _uuid_metadata(raw_source_id)
            if raw_source_id and (source_id is None or source_id in retired_sources):
                continue
            result[asset_id] = hit
            break
    return result


def _paper_group_domain_candidate_ids(
    session: Any,
    *,
    domain: str,
    include_independent_repositories: bool,
) -> list[str]:
    """Return the index-visible members of domain-qualified paper units."""

    memberships = domain_membership(session)
    links = confirmed_links(session)
    domain_ids = select(memberships.c.asset_id).where(memberships.c.slug == domain)
    papers = (
        select(Asset.id)
        .where(
            visible_clause(session),
            Asset.asset_type == "paper",
            (Asset.id.in_(domain_ids))
            | Asset.id.in_(select(links.c.paper_id).where(links.c.repository_id.in_(domain_ids))),
        )
        .cte("domain_papers")
    )
    candidate_ids = set(session.scalars(select(papers.c.id)))
    candidate_ids.update(
        session.scalars(
            select(links.c.repository_id).where(links.c.paper_id.in_(select(papers.c.id)))
        )
    )
    if include_independent_repositories:
        candidate_ids.update(
            session.scalars(
                select(Asset.id).where(
                    visible_clause(session),
                    Asset.asset_type == "repository",
                    Asset.id.in_(domain_ids),
                )
            )
        )
    return sorted(str(asset_id) for asset_id in candidate_ids)


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        visible = visible_clause(session)
        index_ready = (
            select(IndexState.id)
            .where(
                IndexState.index_name == "documents",
                IndexState.entity_type == "asset",
                IndexState.entity_id == Asset.id,
                IndexState.status == "ready",
            )
            .exists()
        )
        counts = session.execute(
            select(
                func.count().label("assets"),
                func.sum(case((Asset.asset_type == "paper", 1), else_=0)).label("papers"),
                *(
                    func.sum(case((reading_status() == value, 1), else_=0)).label(value)
                    for value in ("unread", "reading", "read")
                ),
                func.sum(case((index_ready, 1), else_=0)).label("indexed"),
            )
            .select_from(Asset)
            .where(visible)
        ).one()
        domains = domain_counts(session, _v1._settings(request), include_recent=False)
        recent_ids = list(
            session.scalars(
                select(Asset.id).where(visible).order_by(Asset.updated_at.desc(), Asset.id).limit(8)
            )
        )
        reading_ids = list(
            session.scalars(
                select(Asset.id)
                .join(
                    ReadingState,
                    (ReadingState.asset_id == Asset.id) & (ReadingState.user_id == "local"),
                )
                .outerjoin(
                    ReadingPosition,
                    (ReadingPosition.asset_id == Asset.id) & (ReadingPosition.user_id == "local"),
                )
                .where(visible, ReadingState.status == "reading")
                .order_by(
                    func.coalesce(ReadingPosition.updated_at, ReadingState.updated_at).desc(),
                    Asset.id,
                )
                .limit(8)
            )
        )
        positions = {
            position.asset_id: position
            for position in session.scalars(
                select(ReadingPosition).where(
                    ReadingPosition.user_id == "local", ReadingPosition.asset_id.in_(reading_ids)
                )
            )
        }
        assets = load_catalog_assets(session, list(dict.fromkeys([*recent_ids, *reading_ids])))
        by_id = {asset.id: asset for asset in assets}
        states, summaries = _asset_context(session, assets)

        def cards(ids: list[UUID], *, resume: bool = False) -> list[dict[str, Any]]:
            from apps.api.routes.workflows import _position_payload

            return [
                {
                    **serialize_catalog_asset(
                        by_id[asset_id],
                        reading_state=states.get(asset_id),
                        annotation_summary=summaries.get(asset_id),
                    ),
                    **(
                        {"reading_position": _position_payload(session, positions.get(asset_id))}
                        if resume
                        else {}
                    ),
                }
                for asset_id in ids
            ]

        relation_count = (
            session.scalar(select(func.count(Relation.id)).where(Relation.deleted_at.is_(None)))
            or 0
        )
        annotation_count = (
            session.scalar(select(func.count(Annotation.id)).where(Annotation.deleted_at.is_(None)))
            or 0
        )
        stats = {
            **{
                name: int(getattr(counts, name) or 0)
                for name in ("assets", "papers", "unread", "reading", "read")
            },
            "domains": len(domains),
            "relations": relation_count,
            "annotations": annotation_count,
        }
        indexed_assets = int(counts.indexed or 0)
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
            "recent_assets": cards(recent_ids),
            "continue_reading": cards(reading_ids, resume=True),
            "domains": domains,
            "index_state": {
                "status": "ready" if indexed_assets else "building",
                "indexed_assets": indexed_assets,
                "pending_assets": max(0, stats["assets"] - indexed_assets),
            },
        }


@router.get("/search")
def search_assets(
    request: Request,
    q: str = "",
    mode: str = Query("keyword", pattern="^(exact|keyword)$"),
    sort: str = Query("date_desc", pattern="^(date_desc|date_asc|relevance)$"),
    group_by: str = Query("none", pattern="^(paper|none)$"),
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    author: str | None = None,
    venue: str | None = Query(None, min_length=1, max_length=200),
    year: int | None = Query(None, ge=1450, le=2200),
    collection_id: UUID | None = None,
    asset_type: str | None = Query(None, alias="type"),
    year_from: int | None = Query(None, ge=1450, le=2200),
    year_to: int | None = Query(None, ge=1450, le=2200),
    date_from: date | None = None,
    date_to: date | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    has_pdf: bool | None = None,
    has_html: bool | None = None,
    has_code: bool | None = None,
    has_guide: bool | None = None,
    has_annotation: bool | None = None,
    include_facets: bool = True,
    cursor: str | None = None,
) -> dict[str, Any]:
    if venue is not None:
        venue = venue.strip()
        if not venue:
            raise ApiProblem(422, "会议字段无效", "venue must not be blank.")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ApiProblem(422, "年份范围无效", "year_from must not exceed year_to.")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ApiProblem(
            422,
            "时间范围无效",
            "date_from must be earlier than or equal to date_to.",
            problem_type="urn:research-asset-hub:problem:invalid-date-range",
        )
    offset = decode_cursor(cursor)
    if collection_id is not None:
        with _v1._database(request).session() as session:
            live_collection(session, collection_id)
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
    runtime_filters = (
        {} if paper_grouping else {key: value for key, value in filters.items() if key != "domain"}
    )
    search_hits: dict[UUID, Any] = {}
    search_hit_candidates: dict[UUID, list[Any]] = defaultdict(list)
    search_detail: dict[str, Any] = {}
    skip_runtime_search = False
    if q.strip():
        try:
            with _v1._database(request).session() as count_session:
                active_asset_count = (
                    count_session.scalar(
                        select(func.count(Asset.id)).where(Asset.deleted_at.is_(None))
                    )
                    or 0
                )
                if paper_grouping and domain:
                    candidate_ids = _paper_group_domain_candidate_ids(
                        count_session,
                        domain=domain,
                        include_independent_repositories=asset_type != "paper",
                    )
                    if candidate_ids:
                        runtime_filters = {"asset_id": candidate_ids}
                    else:
                        skip_runtime_search = True
            # Keep a fixed candidate set across pages. Date sorting after
            # retrieval must not change earlier pages when the offset grows.
            search_window = max(100, active_asset_count)
            search_options: dict[str, Any] = {"candidate_limit": search_window}
            if skip_runtime_search:
                search_detail = {"degraded": False, "errors": [], "mode": mode}
            else:
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
                    search_hit_candidates[asset_id].append(hit)
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
        search_hits = _filter_search_hits_for_serving(session, search_hit_candidates)
        if search_hits and not session.scalar(
            select(Asset.id).where(visible_clause(session), Asset.id.in_(search_hits)).limit(1)
        ):
            search_hits.clear()
            search_detail = {
                "degraded": True,
                "errors": ["keyword-runtime:stale-or-foreign-index"],
                "detail": "PostgreSQL metadata fallback is active.",
            }
        memberships = domain_membership(session)
        links = confirmed_links(session)
        selected = catalog_selection(
            session,
            memberships=memberships,
            links=links,
            grouped=paper_grouping,
            query=q,
            matched_ids=list(search_hits) if search_hits else None,
            tokens=_tokens(q),
            domain=domain,
            asset_type=asset_type,
            status=status,
            author=author,
            venue=venue,
            year=year,
            collection_id=collection_id,
            year_from=year_from,
            year_to=year_to,
            date_from=date_from,
            date_to=date_to,
            tags=tag,
            has_pdf=has_pdf,
            has_html=has_html,
            has_code=has_code,
            has_guide=has_guide,
            has_annotation=has_annotation,
        )
        ordered = sorted_selection(
            session,
            selected,
            sort=sort,
            grouped=paper_grouping,
            query=q,
            hits=search_hits,
            links=links,
        )
        if include_facets:
            facets = catalog_facets(session, selected, memberships)
            total = sum(facets["types"].values())
            page_ids = list(session.scalars(ordered.offset(offset).limit(PAGE_SIZE)))
        else:
            rows = session.execute(
                ordered.add_columns(func.count().over().label("total"))
                .offset(offset)
                .limit(PAGE_SIZE)
            ).all()
            page_ids = [row.id for row in rows]
            if rows:
                total = rows[0].total
            elif offset:
                total = session.scalar(select(func.count()).select_from(selected.subquery())) or 0
            else:
                total = 0
        context_ids = list(page_ids)
        if paper_grouping and page_ids:
            context_ids.extend(
                session.scalars(select(links.c.repository_id).where(links.c.paper_id.in_(page_ids)))
            )
            context_ids = list(dict.fromkeys(context_ids))
        context_assets = load_catalog_assets(session, context_ids)
        states, summaries = _asset_context(session, context_assets)
        representations: dict[UUID, list[Representation]] = defaultdict(list)
        if context_ids:
            for representation in session.scalars(
                select(Representation)
                .where(
                    Representation.deleted_at.is_(None),
                    Representation.asset_id.in_(context_ids),
                    active_representation_clause(),
                )
                .order_by(
                    Representation.asset_id,
                    Representation.role,
                    Representation.representation_type,
                    Representation.id,
                )
            ):
                representations[representation.asset_id].append(representation)
        by_id = {asset.id: asset for asset in context_assets}
        page = [by_id[asset_id] for asset_id in page_ids]
        if paper_grouping:
            if q.strip():
                matched_ids = (
                    set(search_hits)
                    if search_hits
                    else set(
                        session.scalars(
                            select(Asset.id).where(
                                Asset.id.in_(context_ids), metadata_match(q, _tokens(q))
                            )
                        )
                    )
                )
                matched_assets = [asset for asset in context_assets if asset.id in matched_ids]
                # Keep the search index's tie order for best-match selection.
                if search_hits:
                    order = {asset_id: position for position, asset_id in enumerate(search_hits)}
                    matched_assets.sort(key=lambda asset: order[asset.id])
            else:
                matched_assets = page
            units, _ = _build_research_units(
                session,
                catalog_assets=context_assets,
                primary_ids=set(page_ids),
                matched_assets=matched_assets,
                search_hits=search_hits,
                query=q,
                states=states,
                summaries=summaries,
                representations=representations,
                include_independent_repositories=bool(q.strip()) and asset_type != "paper",
                source_registry=_v1._source_registry(request),
            )
            units_by_id = {unit["unit_id"]: unit for unit in units}
            items = [units_by_id[str(asset_id)] for asset_id in page_ids]
        else:
            items = []
            for asset in page:
                selected_hit = search_hits.get(asset.id)
                item = serialize_catalog_asset(
                    asset,
                    reading_state=states.get(asset.id),
                    annotation_summary=summaries.get(asset.id),
                    score=round(float(selected_hit.score), 6) if selected_hit is not None else 1.0,
                )
                if selected_hit is not None:
                    item.update(
                        snippet=" ".join(selected_hit.document.text.split())[:360],
                        document_kind=selected_hit.document.kind,
                        anchor=selected_hit.document.metadata.get("anchor"),
                        representation_id=selected_hit.document.metadata.get("representation_id"),
                        search_source=selected_hit.source,
                        exact_match=selected_hit.exact_match,
                    )
                item["representations"] = [
                    serialize_representation(representation)
                    for representation in representations.get(asset.id, [])
                    if representation.availability == "available"
                ]
                items.append(item)
        payload = _list_payload(items, offset, total)
        payload.update(mode=mode, sort=sort)
        if include_facets:
            payload["facets"] = facets
        if paper_grouping:
            payload["group_by"] = "paper"
        payload.update(search_detail)
        payload.setdefault("degraded", bool(q.strip() and not search_hits))
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
        statement = select(Asset.id).where(~func.coalesce(hidden_clause(session), False))
        if not include_deleted:
            statement = statement.where(Asset.deleted_at.is_(None))
        if domain:
            memberships = domain_membership(session)
            statement = statement.where(
                Asset.id.in_(select(memberships.c.asset_id).where(memberships.c.slug == domain))
            )
        if status:
            statement = statement.where(reading_status() == status)
        total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        page_ids = list(
            session.scalars(
                statement.order_by(Asset.updated_at.desc(), Asset.id)
                .offset(offset)
                .limit(PAGE_SIZE)
            )
        )
        page = load_catalog_assets(session, page_ids)
        states, summaries = _asset_context(session, page)
        items = [
            serialize_catalog_asset(
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
                        active_representation_clause(),
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
        return _list_payload(items, offset, total)


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
        annotate_presentation_domains(session, [asset])
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
        related_repository_ids = set(
            session.scalars(
                select(Relation.object_asset_id).where(
                    Relation.subject_asset_id == asset.id,
                    Relation.relation_type == "implemented_by",
                    Relation.confirmation_status == "confirmed",
                    Relation.deleted_at.is_(None),
                )
            ).all()
        )
        related_repositories = (
            list(
                session.scalars(
                    select(Asset)
                    .where(
                        Asset.id.in_(related_repository_ids),
                        Asset.asset_type == "repository",
                        Asset.deleted_at.is_(None),
                    )
                    .order_by(Asset.title, Asset.id)
                ).all()
            )
            if related_repository_ids
            else []
        )
        annotate_presentation_domains(session, [asset, *related_repositories])
        member_ids = {asset.id, *(item.id for item in related_repositories)}
        member_representations: dict[UUID, list[Representation]] = defaultdict(list)
        for representation in session.scalars(
            select(Representation)
            .where(
                Representation.asset_id.in_(member_ids),
                Representation.deleted_at.is_(None),
                active_representation_clause(),
            )
            .order_by(
                Representation.asset_id,
                Representation.role,
                Representation.representation_type,
                Representation.id,
            )
        ):
            member_representations[representation.asset_id].append(representation)
        result["representations"] = [
            serialize_representation(item) for item in member_representations.get(asset.id, [])
        ]
        source_aliases = {
            source.id: source.alias for source in session.scalars(select(Source)).all()
        }
        result["related_repositories"] = [
            serialize_asset(repository) for repository in related_repositories
        ]
        result["quick_actions"] = _research_unit_quick_actions(
            asset,
            related_repositories,
            member_representations,
            source_registry=_v1._source_registry(request),
            source_aliases=source_aliases,
        )
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
def list_representations(
    asset_id: UUID,
    request: Request,
    include_retired: bool = False,
) -> dict[str, Any]:
    """List serving representations, with an explicit path-free audit opt-in."""

    with _v1._database(request).session() as session:
        asset = _live_asset(session, asset_id)
        statement = select(Representation).where(
            Representation.asset_id == asset.id,
            Representation.deleted_at.is_(None),
        )
        if not include_retired:
            statement = statement.where(active_representation_clause())
        items = list(
            session.scalars(
                statement.order_by(
                    Representation.role,
                    Representation.representation_type,
                )
            ).all()
        )
        source_aliases = {
            source.id: source.alias
            for source in session.scalars(
                select(Source).where(
                    Source.id.in_({item.source_id for item in items if item.source_id is not None})
                )
            ).all()
        }
        serialized_items: list[dict[str, Any]] = []
        for item in items:
            serving = representation_serving_status(session, item)
            if serving.active:
                serialized_items.append(
                    _serialize_representation_for_ui(
                        request,
                        item,
                        source_alias=(
                            source_aliases.get(item.source_id)
                            if item.source_id is not None
                            else None
                        ),
                    )
                )
                continue
            # Audit mode deliberately omits every location-bearing field and
            # never derives an editor/open action from the historical source.
            serialized_items.append(
                {
                    "id": str(item.id),
                    "asset_id": str(item.asset_id),
                    "source_id": str(item.source_id) if item.source_id else None,
                    "representation_type": item.representation_type,
                    "type": item.representation_type,
                    "role": item.role,
                    "materialization_kind": item.materialization_kind,
                    "availability": item.availability,
                    "retired": serving.retirement_id is not None,
                    "excluded": serving.exclusion_id is not None,
                    "audit_only": True,
                    "successor_representation_id": (
                        str(serving.successor_representation_id)
                        if serving.successor_representation_id is not None
                        else None
                    ),
                    "path_ref": None,
                    "external_url": None,
                    "entrypoint": None,
                    "open_actions": [],
                }
            )
        return {
            "items": serialized_items,
            "next_cursor": None,
            "total": len(serialized_items),
            "include_retired": include_retired,
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
        try:
            assert_representation_active(session, representation)
        except RetiredRepresentationError as error:
            raise _retired_representation_problem(error) from error
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
