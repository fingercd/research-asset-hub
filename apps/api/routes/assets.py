"""领域路由：资产、搜索、仪表盘与表示。"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Body, Header, Query, Request, Response
from sqlalchemy import func, select

from apps.api import routes_v1 as _v1
from apps.api.code_browser import CodeBrowserPathError, list_code_directory, read_code_file
from apps.api.contracts import AssetCreate, AssetPatch, ReadingStateUpdate
from apps.api.domain_views import annotate_presentation_domains, presentation_domains
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
        if (
            representation_id := _uuid_metadata(
                hit.document.metadata.get("representation_id")
            )
        )
        is not None
    }
    source_ids = {
        source_id
        for hits in candidates.values()
        for hit in hits
        if (source_id := _uuid_metadata(hit.document.metadata.get("source_id")))
        is not None
    }
    active_representations = active_representation_ids(session, representation_ids)
    retired_sources = retired_source_ids(session, source_ids)
    result: dict[UUID, Any] = {}
    for asset_id, hits in candidates.items():
        for hit in hits:
            raw_representation_id = hit.document.metadata.get("representation_id")
            representation_id = _uuid_metadata(raw_representation_id)
            if raw_representation_id and (
                representation_id is None
                or representation_id not in active_representations
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

    catalog_assets = [
        asset
        for asset in session.scalars(
            select(Asset).where(Asset.deleted_at.is_(None))
        ).all()
        if not _is_system_hidden_asset(asset)
    ]
    annotate_presentation_domains(session, catalog_assets)
    assets_by_id = {asset.id: asset for asset in catalog_assets}
    paper_repository_ids: dict[UUID, set[UUID]] = defaultdict(set)
    for relation in session.scalars(
        select(Relation).where(
            Relation.relation_type == "implemented_by",
            Relation.confirmation_status == "confirmed",
            Relation.deleted_at.is_(None),
        )
    ):
        paper = assets_by_id.get(relation.subject_asset_id)
        repository = assets_by_id.get(relation.object_asset_id)
        if (
            paper is not None
            and paper.asset_type == "paper"
            and repository is not None
            and repository.asset_type == "repository"
        ):
            paper_repository_ids[paper.id].add(repository.id)

    candidate_ids: set[UUID] = set()
    for paper in catalog_assets:
        if paper.asset_type != "paper":
            continue
        repository_ids = paper_repository_ids.get(paper.id, set())
        related_repositories = [
            assets_by_id[repository_id] for repository_id in repository_ids
        ]
        if domain not in presentation_domains(paper) and not any(
            domain in presentation_domains(repository)
            for repository in related_repositories
        ):
            continue
        candidate_ids.add(paper.id)
        candidate_ids.update(repository_ids)

    if include_independent_repositories:
        candidate_ids.update(
            asset.id
            for asset in catalog_assets
            if asset.asset_type == "repository" and domain in presentation_domains(asset)
        )
    return sorted(str(asset_id) for asset_id in candidate_ids)


def _first_public_date(asset: Asset) -> date | None:
    """Return the canonical, day-precision publication date when available."""

    value = (asset.attributes or {}).get("first_public_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(normalized.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _matches_publication_date_range(
    asset: Asset,
    *,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    if date_from is None and date_to is None:
        return True
    published = _first_public_date(asset)
    if published is None:
        return False
    return not (
        (date_from is not None and published < date_from)
        or (date_to is not None and published > date_to)
    )


def _date_sort_values(asset: Asset) -> tuple[int, int, int]:
    """Build a deterministic publication-date key with documented fallbacks."""

    published = _first_public_date(asset)
    if published is not None:
        primary = published.toordinal() * 86_400_000_000
    elif asset.year is not None:
        primary = date(asset.year, 1, 1).toordinal() * 86_400_000_000
    elif asset.created_at is not None:
        created = asset.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        primary = (
            created.date().toordinal() * 86_400_000_000
            + created.hour * 3_600_000_000
            + created.minute * 60_000_000
            + created.second * 1_000_000
            + created.microsecond
        )
    else:
        primary = 0
    created_at = asset.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    created_value = (
        created_at.date().toordinal() * 86_400_000_000
        + created_at.hour * 3_600_000_000
        + created_at.minute * 60_000_000
        + created_at.second * 1_000_000
        + created_at.microsecond
        if created_at is not None
        else 0
    )
    return (
        primary,
        asset.year or 0,
        created_value,
    )


def _sort_assets_by_date(assets: list[Asset], *, descending: bool) -> list[Asset]:
    direction = -1 if descending else 1
    return sorted(
        assets,
        key=lambda asset: (
            *(direction * value for value in _date_sort_values(asset)),
            str(asset.id),
        ),
    )


def _title_query_priority(asset: Asset, query: str) -> int:
    normalized = " ".join(query.casefold().split())
    if not normalized:
        return 1
    normalized_titles = [
        " ".join(value.casefold().split())
        for value in (asset.title, asset.title_zh or "")
        if value
    ]
    if normalized in normalized_titles:
        return 0
    if any(normalized in value for value in normalized_titles):
        return 1
    return 2


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        all_assets = session.scalars(
            select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.updated_at.desc())
        ).all()
        assets = [
            asset
            for asset in all_assets
            if not _is_system_hidden_asset(asset)
        ]
        states, summaries = _asset_context(session, assets)
        reading_counts = {"unread": 0, "reading": 0, "read": 0}
        for asset in assets:
            reading_state = states.get(asset.id)
            status = reading_state.status if reading_state is not None else "unread"
            reading_counts[status] = reading_counts.get(status, 0) + 1
        domains = _domains_with_counts(
            session,
            _v1._settings(request),
            assets,
            all_assets=list(all_assets),
            asset_context=(states, summaries),
        )
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
        indexed_assets = session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.id.in_([asset.id for asset in assets]),
                select(IndexState.id)
                .where(
                    IndexState.index_name == "documents",
                    IndexState.entity_type == "asset",
                    IndexState.entity_id == Asset.id,
                    IndexState.status == "ready",
                )
                .exists(),
            )
        ) or 0
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
    sort: str = Query("date_desc", pattern="^(date_desc|date_asc|relevance)$"),
    group_by: str = Query("none", pattern="^(paper|none)$"),
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    author: str | None = None,
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
    cursor: str | None = None,
) -> dict[str, Any]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ApiProblem(
            422,
            "时间范围无效",
            "date_from must be earlier than or equal to date_to.",
            problem_type="urn:research-asset-hub:problem:invalid-date-range",
        )
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
    runtime_filters = (
        {}
        if paper_grouping
        else {key: value for key, value in filters.items() if key != "domain"}
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
        catalog_assets = [
            asset
            for asset in session.scalars(
                select(Asset)
                .where(Asset.deleted_at.is_(None))
                .order_by(Asset.updated_at.desc(), Asset.id)
            ).all()
            if not _is_system_hidden_asset(asset)
        ]
        annotate_presentation_domains(session, catalog_assets)
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
        filtered: list[Asset] = []
        for asset in assets:
            if not paper_grouping:
                if domain and domain not in presentation_domains(asset):
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
                if not _matches_publication_date_range(
                    asset,
                    date_from=date_from,
                    date_to=date_to,
                ):
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
                # The default catalog is a paper census: repository-only assets
                # remain searchable, but must not inflate the no-query paper total.
                include_independent_repositories=bool(q.strip())
                and asset_type != "paper",
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
            unit_pairs = [
                (unit, asset)
                for unit, asset in zip(units, facet_assets, strict=True)
                if _matches_publication_date_range(
                    asset,
                    date_from=date_from,
                    date_to=date_to,
                )
            ]
            if sort != "relevance":
                direction = -1 if sort == "date_desc" else 1
                unit_pairs.sort(
                    key=lambda item: (
                        _title_query_priority(item[1], q),
                        *(direction * value for value in _date_sort_values(item[1])),
                        str(item[1].id),
                    )
                )
            units = [unit for unit, _ in unit_pairs]
            facet_assets = [asset for _, asset in unit_pairs]
            payload = _list_payload(units[offset : offset + PAGE_SIZE], offset, len(units))
            payload["mode"] = mode
            payload["sort"] = sort
            payload["group_by"] = "paper"
            payload.update(search_detail)
            payload.setdefault("degraded", bool(q.strip() and not search_hits))
            payload["facets"] = _asset_facets(facet_assets, states)
            return payload

        if sort != "relevance":
            filtered = _sort_assets_by_date(
                filtered,
                descending=sort == "date_desc",
            )
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
        payload["sort"] = sort
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
        annotate_presentation_domains(session, assets)
        states, summaries = _asset_context(session, assets)
        filtered: list[Asset] = []
        for asset in assets:
            if domain and domain not in presentation_domains(asset):
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
            serialize_representation(item)
            for item in member_representations.get(asset.id, [])
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
