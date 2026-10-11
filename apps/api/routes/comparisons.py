"""领域路由：论文比较与相似推荐。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID, uuid4

from fastapi import Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import routes_v1 as _v1
from apps.api.catalog_queries import confirmed_links
from apps.api.contracts import ComparisonCreate
from apps.api.domain_views import annotate_presentation_domains
from apps.api.events import record_mutation, replay_idempotent
from apps.api.problem import ApiProblem
from apps.api.routes_v1 import (
    _asset_context,
    _comparison_columns,
    _is_system_hidden_asset,
    _live_asset,
    _request_id,
    _research_unit_quick_actions,
    _serialize_assets,
    _tokens,
    router,
)
from apps.api.serialization import serialize_asset, serialize_comparison, serialize_representation
from packages.domain.enums import AuditAction
from packages.domain.models import Asset, Representation, SavedView, Source
from packages.domain.serving_scope import active_representation_clause


def _comparison_assets(
    session: Session, request: Request, assets: list[Asset],
) -> list[dict[str, Any]]:
    """Load the same serving code actions as asset details, in one batch."""
    if not assets:
        return []
    links = confirmed_links(session)
    repository_ids: dict[UUID, set[UUID]] = defaultdict(set)
    for paper_id, repository_id in session.execute(
        select(links.c.paper_id, links.c.repository_id)
        .where(links.c.paper_id.in_([asset.id for asset in assets]))
    ):
        repository_ids[paper_id].add(repository_id)
    all_repository_ids = {item for ids in repository_ids.values() for item in ids}
    repositories = {
        item.id: item for item in session.scalars(
            select(Asset).where(Asset.id.in_(all_repository_ids))
            .order_by(Asset.title, Asset.id)
        )
    }
    members = {asset.id: asset for asset in [*assets, *repositories.values()]}
    annotate_presentation_domains(session, list(members.values()))
    representations: dict[UUID, list[Representation]] = defaultdict(list)
    for item in session.scalars(
        select(Representation).where(
            Representation.asset_id.in_(members),
            Representation.deleted_at.is_(None),
            active_representation_clause(),
        ).order_by(
            Representation.asset_id, Representation.role,
            Representation.representation_type, Representation.id,
        )
    ):
        representations[item.asset_id].append(item)
    aliases: dict[UUID, str] = {
        source_id: alias for source_id, alias in session.execute(select(Source.id, Source.alias))
    }
    registry = _v1._source_registry(request)
    results = _serialize_assets(session, assets)
    for asset, result in zip(assets, results, strict=True):
        related = [
            repository for repository in repositories.values()
            if repository.id in repository_ids[asset.id]
        ]
        result["representations"] = [
            serialize_representation(item) for item in representations[asset.id]
        ]
        result["related_repositories"] = [
            {
                **serialize_asset(repository),
                "representations": [
                    serialize_representation(item) for item in representations[repository.id]
                ],
            }
            for repository in related
        ]
        result["quick_actions"] = _research_unit_quick_actions(
            asset, related, representations,
            source_registry=registry, source_aliases=aliases,
        )
    return results


@router.get("/comparisons")
def list_comparisons(request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        views = list(
            session.scalars(
                select(SavedView)
                .where(
                    SavedView.user_id == "local",
                    SavedView.view_kind == "comparison",
                    SavedView.deleted_at.is_(None),
                )
                .order_by(SavedView.updated_at.desc())
            ).all()
        )
        items: list[dict[str, Any]] = []
        all_ids = {
            UUID(value) for view in views for value in view.definition.get("asset_ids", [])
        }
        all_assets = [
            asset for asset in session.scalars(
                select(Asset).where(Asset.id.in_(all_ids), Asset.deleted_at.is_(None))
            ) if not _is_system_hidden_asset(asset)
        ]
        projections = {
            item["id"]: item for item in _comparison_assets(session, request, all_assets)
        }
        for view in views:
            assets = [
                projections[value] for value in view.definition.get("asset_ids", [])
                if value in projections
            ]
            items.append(serialize_comparison(view, assets))
        return {"items": items, "next_cursor": None, "total": len(items)}

@router.post("/comparisons", status_code=201)
def create_comparison(
    payload: ComparisonCreate,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if len(set(payload.asset_ids)) != len(payload.asset_ids):
        raise ApiProblem(
            422,
            "论文选择无效",
            "A comparison cannot contain the same asset more than once.",
            problem_type="urn:research-asset-hub:problem:validation",
        )
    request_payload = payload.model_dump(mode="json")
    with _v1._database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="create-comparison",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        assets = [_live_asset(session, asset_id) for asset_id in payload.asset_ids]
        title = payload.title or " vs. ".join(item.title[:40] for item in assets)
        existing_view = session.scalar(
            select(SavedView).where(
                SavedView.user_id == "local",
                SavedView.view_kind == "comparison",
                SavedView.name == title[:255],
                SavedView.deleted_at.is_(None),
            )
        )
        requested_ids = [str(item.id) for item in assets]
        if existing_view is not None and existing_view.definition.get("asset_ids") == requested_ids:
            result = serialize_comparison(
                existing_view,
                _comparison_assets(session, request, assets),
            )
            if idempotency_key:
                record_mutation(
                    session,
                    entity_type="saved_view",
                    entity_id=existing_view.id,
                    action=AuditAction.UPDATE,
                    before=result,
                    after=result,
                    request_id=_request_id(request),
                    event_type="comparison.reused",
                    idempotency_scope="create-comparison",
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                    response_payload=result,
                )
            return result
        if existing_view is not None:
            title = f"{title[:240]} · {uuid4().hex[:8]}"
        definition = {
            "asset_ids": requested_ids,
            "columns": _comparison_columns(assets),
        }
        view = SavedView(
            user_id="local",
            view_kind="comparison",
            name=title[:255],
            definition=definition,
        )
        session.add(view)
        session.flush()
        result = serialize_comparison(view, _comparison_assets(session, request, assets))
        record_mutation(
            session,
            entity_type="saved_view",
            entity_id=view.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="comparison.created",
            idempotency_scope="create-comparison",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        return result

@router.get("/similar/{asset_id}")
def similar_assets(asset_id: UUID, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        target = _live_asset(session, asset_id)
        try:
            keyword = _v1._search_runtime(request).service.search(
                "\n".join(
                    value
                    for value in (
                        target.title,
                        target.title_zh,
                        target.abstract,
                        target.summary,
                    )
                    if value
                ),
                mode="keyword",
                filters={"type": "paper"},
                limit=60,
            )
        except Exception:
            keyword = None
        if keyword is not None:
            ranked_ids: list[UUID] = []
            hit_by_asset: dict[UUID, Any] = {}
            for hit in keyword.hits:
                try:
                    candidate_id = UUID(hit.document.asset_id)
                except ValueError:
                    continue
                if candidate_id == target.id or candidate_id in hit_by_asset:
                    continue
                hit_by_asset[candidate_id] = hit
                ranked_ids.append(candidate_id)
                if len(ranked_ids) >= 20:
                    break
            indexed = {
                item.id: item
                for item in session.scalars(
                    select(Asset).where(
                        Asset.id.in_(ranked_ids),
                        Asset.deleted_at.is_(None),
                        Asset.asset_type == "paper",
                    )
                )
            }
            values = [indexed[item] for item in ranked_ids if item in indexed]
            if values:
                states, summaries = _asset_context(session, values)
                return {
                    "items": [
                        serialize_asset(
                            item,
                            reading_state=states.get(item.id),
                            annotation_summary=summaries.get(item.id),
                            score=round(float(hit_by_asset[item.id].score), 6),
                        )
                        for item in values
                    ],
                    "next_cursor": None,
                    "total": len(values),
                    "method": "keyword-meilisearch",
                    "degraded": keyword.degraded,
                    "errors": list(keyword.errors),
                }
        candidates = list(
            session.scalars(
                select(Asset).where(
                    Asset.deleted_at.is_(None),
                    Asset.id != target.id,
                )
            ).all()
        )
        target_terms = _tokens(f"{target.title} {target.title_zh or ''} {target.summary or ''}")
        target_domains = set(target.domain_slugs or [])
        target_tags = set(target.tags or [])
        ranked: list[tuple[float, Asset]] = []
        for item in candidates:
            terms = _tokens(f"{item.title} {item.title_zh or ''} {item.summary or ''}")
            term_score = len(target_terms & terms) / max(1, len(target_terms | terms))
            domain_score = len(target_domains & set(item.domain_slugs or []))
            tag_score = len(target_tags & set(item.tags or []))
            year_score = (
                max(0.0, 1.0 - abs(item.year - target.year) / 10)
                if item.year is not None and target.year is not None
                else 0.0
            )
            score = term_score * 3 + domain_score * 2 + tag_score + year_score * 0.25
            if score > 0:
                ranked.append((score, item))
        ranked.sort(key=lambda value: value[0], reverse=True)
        states, summaries = _asset_context(
            session,
            [asset for _, asset in ranked[:20]],
        )
        items = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
                score=round(score, 4),
            )
            for score, item in ranked[:20]
        ]
        return {
            "items": items,
            "next_cursor": None,
            "total": len(items),
            "method": "metadata-fallback",
            "degraded": True,
        }
