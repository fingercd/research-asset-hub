"""领域路由：关系图谱。"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Annotated, Any
from uuid import UUID

from fastapi import Query, Request
from sqlalchemy import and_, func, or_, select

from apps.api import routes_v1 as _v1
from apps.api.routes_v1 import (
    _asset_context,
    _graph_payload,
    _live_asset,
    router,
)
from apps.api.serialization import serialize_asset
from packages.domain.models import Asset, Relation


@router.get("/graph/neighbors")
def graph_neighbors(
    request: Request,
    asset_id: UUID,
    depth: int = Query(1, ge=1, le=3),
    relation_type: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        asset_id = _live_asset(session, asset_id).id
        all_relations = list(
            session.scalars(
                select(Relation).where(
                    Relation.deleted_at.is_(None),
                    *([Relation.relation_type == relation_type] if relation_type else []),
                    *(
                        [Relation.confirmation_status == relation_status]
                        if relation_status != "all"
                        else []
                    ),
                )
            ).all()
        )
        visited = {asset_id}
        frontier = {asset_id}
        chosen: list[Relation] = []
        for _ in range(depth):
            next_frontier: set[UUID] = set()
            for relation in all_relations:
                if relation.subject_asset_id in frontier or relation.object_asset_id in frontier:
                    chosen.append(relation)
                    next_frontier.update([relation.subject_asset_id, relation.object_asset_id])
            next_frontier -= visited
            visited |= next_frontier
            frontier = next_frontier
            if not frontier or len(visited) >= 1_000:
                break
        unique_relations = list({item.id: item for item in chosen}.values())
        if status:
            states, _ = _asset_context(
                session,
                list(session.scalars(select(Asset).where(Asset.id.in_(visited))).all()),
            )
            filtered = {
                node_id
                for node_id in visited
                if node_id == asset_id
                or (states.get(node_id) is not None and states[node_id].status == status)
                or (status == "unread" and states.get(node_id) is None)
            }
            unique_relations = [
                item
                for item in unique_relations
                if item.subject_asset_id in filtered and item.object_asset_id in filtered
            ]
            visited = filtered
        return _graph_payload(
            session,
            visited,
            unique_relations,
            truncated=len(visited) >= 1_000,
        )

@router.get("/graph/path")
def graph_path(
    request: Request,
    from_asset: Annotated[UUID, Query(alias="from")],
    to_asset: Annotated[UUID, Query(alias="to")],
    max_depth: int = Query(3, ge=1, le=6),
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        from_asset = _live_asset(session, from_asset).id
        to_asset = _live_asset(session, to_asset).id
        relation_statement = select(Relation).where(Relation.deleted_at.is_(None))
        if relation_status != "all":
            relation_statement = relation_statement.where(
                Relation.confirmation_status == relation_status
            )
        relations = list(session.scalars(relation_statement).all())
        adjacency: dict[UUID, list[tuple[UUID, Relation]]] = defaultdict(list)
        for relation in relations:
            adjacency[relation.subject_asset_id].append((relation.object_asset_id, relation))
            adjacency[relation.object_asset_id].append((relation.subject_asset_id, relation))
        queue: deque[UUID] = deque([from_asset])
        parent: dict[UUID, tuple[UUID, Relation] | None] = {from_asset: None}
        while queue and to_asset not in parent:
            current = queue.popleft()
            current_depth = 0
            cursor = current
            while parent[cursor] is not None:
                cursor_link = parent[cursor]
                if cursor_link is None:
                    break
                cursor = cursor_link[0]
                current_depth += 1
            if current_depth >= max_depth:
                continue
            for neighbor, relation in adjacency[current]:
                if neighbor not in parent:
                    parent[neighbor] = (current, relation)
                    queue.append(neighbor)
        if to_asset not in parent:
            return {"nodes": [], "edges": [], "truncated": False}
        node_ids = {to_asset}
        selected: list[Relation] = []
        current = to_asset
        parent_link = parent[current]
        while parent_link is not None:
            previous_node, relation = parent_link
            node_ids.add(previous_node)
            selected.append(relation)
            current = previous_node
            parent_link = parent[current]
        selected.reverse()
        return _graph_payload(session, node_ids, selected)

@router.get("/graph/common-neighbors")
def graph_common_neighbors(
    request: Request,
    left: UUID,
    right: UUID,
    relation_type: str | None = None,
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        left = _live_asset(session, left).id
        right = _live_asset(session, right).id
        relations = list(
            session.scalars(
                select(Relation).where(
                    Relation.deleted_at.is_(None),
                    *([Relation.relation_type == relation_type] if relation_type else []),
                    *(
                        [Relation.confirmation_status == relation_status]
                        if relation_status != "all"
                        else []
                    ),
                )
            ).all()
        )
        adjacency: dict[UUID, set[UUID]] = defaultdict(set)
        for relation in relations:
            adjacency[relation.subject_asset_id].add(relation.object_asset_id)
            adjacency[relation.object_asset_id].add(relation.subject_asset_id)
        common = adjacency[left] & adjacency[right]
        selected = [
            relation
            for relation in relations
            if (relation.subject_asset_id in {left, right} and relation.object_asset_id in common)
            or (relation.object_asset_id in {left, right} and relation.subject_asset_id in common)
        ]
        return {
            **_graph_payload(
                session,
                {left, right, *common},
                selected,
            ),
            "common_neighbor_ids": [str(item) for item in sorted(common, key=str)],
        }

@router.get("/graph/timeline")
def graph_timeline(
    request: Request,
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        assets = list(
            session.scalars(
                select(Asset)
                .where(
                    Asset.deleted_at.is_(None),
                    Asset.asset_type == "paper",
                )
                .order_by(Asset.year.asc().nullslast(), Asset.title, Asset.id)
            ).all()
        )
        if domain:
            assets = [asset for asset in assets if domain in (asset.domain_slugs or [])]
        states, summaries = _asset_context(session, assets)
        if status:
            assets = [
                asset
                for asset in assets
                if (states[asset.id].status if asset.id in states else "unread") == status
            ]
        relation_counts = {
            asset_id: int(count)
            for asset_id, count in session.execute(
                select(
                    Asset.id,
                    func.count(Relation.id),
                )
                .outerjoin(
                    Relation,
                    and_(
                        or_(
                            Relation.subject_asset_id == Asset.id,
                            Relation.object_asset_id == Asset.id,
                        ),
                        Relation.deleted_at.is_(None),
                        *(
                            [Relation.confirmation_status == relation_status]
                            if relation_status != "all"
                            else []
                        ),
                    ),
                )
                .where(Asset.id.in_([asset.id for asset in assets]))
                .group_by(Asset.id)
            )
        }
        items = [
            {
                **serialize_asset(
                    asset,
                    reading_state=states.get(asset.id),
                    annotation_summary=summaries.get(asset.id),
                ),
                "relation_count": relation_counts.get(asset.id, 0),
            }
            for asset in assets
        ]
        return {
            "items": items,
            "next_cursor": None,
            "total": len(items),
        }
