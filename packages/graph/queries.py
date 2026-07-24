"""Bounded graph algorithms over PostgreSQL or in-memory relation stores."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class RelationEdge:
    edge_id: str
    subject_id: str
    object_id: str
    relation_type: str
    confidence: float | None = None
    confirmation_status: str = "candidate"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphPath:
    nodes: tuple[str, ...]
    edges: tuple[RelationEdge, ...]


class RelationStore(Protocol):
    def edges_for(
        self,
        node_ids: Sequence[str],
        *,
        relation_types: Sequence[str] | None = None,
    ) -> list[RelationEdge]: ...


class PostgresExecutor(Protocol):
    def fetch_all(
        self,
        query: str,
        params: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]: ...


class PostgresRelationStore:
    """PostgreSQL-specific edge fetch using bounded UUID-array predicates."""

    QUERY = """
        SELECT id, subject_asset_id, object_asset_id, relation_type,
               confidence, confirmation_status, evidence
        FROM relations
        WHERE deleted_at IS NULL
          AND (
            subject_asset_id = ANY(%(node_ids)s::uuid[])
            OR object_asset_id = ANY(%(node_ids)s::uuid[])
          )
          AND (
            %(relation_types)s::text[] IS NULL
            OR relation_type = ANY(%(relation_types)s::text[])
          )
        ORDER BY id
        LIMIT %(edge_limit)s
    """

    def __init__(self, executor: PostgresExecutor, *, edge_limit: int = 10_000) -> None:
        self.executor = executor
        self.edge_limit = edge_limit

    def edges_for(
        self,
        node_ids: Sequence[str],
        *,
        relation_types: Sequence[str] | None = None,
    ) -> list[RelationEdge]:
        if not node_ids:
            return []
        rows = self.executor.fetch_all(
            self.QUERY,
            {
                "node_ids": list(dict.fromkeys(node_ids)),
                "relation_types": list(relation_types) if relation_types else None,
                "edge_limit": self.edge_limit,
            },
        )
        return [
            RelationEdge(
                edge_id=str(row["id"]),
                subject_id=str(row["subject_asset_id"]),
                object_id=str(row["object_asset_id"]),
                relation_type=str(row["relation_type"]),
                confidence=(
                    float(row["confidence"])
                    if row.get("confidence") is not None
                    else None
                ),
                confirmation_status=str(
                    row.get("confirmation_status", "candidate")
                ),
                evidence=(
                    dict(row["evidence"])
                    if isinstance(row.get("evidence"), Mapping)
                    else {}
                ),
            )
            for row in rows
        ]


class InMemoryRelationStore:
    def __init__(self, edges: Sequence[RelationEdge] = ()) -> None:
        self.edges = list(edges)

    def edges_for(
        self,
        node_ids: Sequence[str],
        *,
        relation_types: Sequence[str] | None = None,
    ) -> list[RelationEdge]:
        selected = set(node_ids)
        allowed = set(relation_types or ())
        return [
            edge
            for edge in self.edges
            if (edge.subject_id in selected or edge.object_id in selected)
            and (not allowed or edge.relation_type in allowed)
        ]


Direction = Literal["outgoing", "incoming", "both"]


class GraphQueries:
    def __init__(self, store: RelationStore, *, max_nodes: int = 1_000) -> None:
        self.store = store
        self.max_nodes = max_nodes

    @staticmethod
    def _step(
        node_id: str,
        edge: RelationEdge,
        direction: Direction,
    ) -> str | None:
        if direction in {"outgoing", "both"} and edge.subject_id == node_id:
            return edge.object_id
        if direction in {"incoming", "both"} and edge.object_id == node_id:
            return edge.subject_id
        return None

    def neighbors(
        self,
        node_id: str,
        *,
        direction: Direction = "both",
        relation_types: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[tuple[str, RelationEdge]]:
        values: list[tuple[str, RelationEdge]] = []
        for edge in self.store.edges_for([node_id], relation_types=relation_types):
            neighbor = self._step(node_id, edge, direction)
            if neighbor is not None:
                values.append((neighbor, edge))
        values.sort(key=lambda value: (value[0], value[1].edge_id))
        return values[:limit]

    def expand(
        self,
        start_id: str,
        *,
        max_depth: int = 3,
        direction: Direction = "both",
        relation_types: Sequence[str] | None = None,
    ) -> tuple[set[str], list[RelationEdge]]:
        if not 0 <= max_depth <= 3:
            raise ValueError("Graph expansion depth must be between 0 and 3")
        seen = {start_id}
        edges: dict[str, RelationEdge] = {}
        frontier = {start_id}
        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            fetched = self.store.edges_for(
                sorted(frontier),
                relation_types=relation_types,
            )
            for edge in fetched:
                for current in tuple(frontier):
                    neighbor = self._step(current, edge, direction)
                    if neighbor is None:
                        continue
                    edges[edge.edge_id] = edge
                    if neighbor not in seen:
                        next_frontier.add(neighbor)
                        seen.add(neighbor)
                        if len(seen) > self.max_nodes:
                            raise ValueError("Graph query exceeded the node limit")
            frontier = next_frontier
        return seen, list(edges.values())

    def shortest_path(
        self,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 3,
        direction: Direction = "both",
        relation_types: Sequence[str] | None = None,
    ) -> GraphPath | None:
        if not 0 <= max_depth <= 3:
            raise ValueError("Shortest-path depth must be between 0 and 3")
        if source_id == target_id:
            return GraphPath((source_id,), ())
        queue: deque[
            tuple[str, tuple[str, ...], tuple[RelationEdge, ...]]
        ] = deque([(source_id, (source_id,), ())])
        seen = {source_id}
        while queue:
            current, nodes, edges = queue.popleft()
            if len(edges) >= max_depth:
                continue
            for neighbor, edge in self.neighbors(
                current,
                direction=direction,
                relation_types=relation_types,
                limit=self.max_nodes,
            ):
                if neighbor in seen:
                    continue
                next_nodes = (*nodes, neighbor)
                next_edges = (*edges, edge)
                if neighbor == target_id:
                    return GraphPath(next_nodes, next_edges)
                seen.add(neighbor)
                if len(seen) > self.max_nodes:
                    raise ValueError("Graph query exceeded the node limit")
                queue.append((neighbor, next_nodes, next_edges))
        return None

    def common_neighbors(
        self,
        left_id: str,
        right_id: str,
        *,
        relation_types: Sequence[str] | None = None,
    ) -> list[str]:
        left = {
            node
            for node, _ in self.neighbors(
                left_id,
                relation_types=relation_types,
                limit=self.max_nodes,
            )
        }
        right = {
            node
            for node, _ in self.neighbors(
                right_id,
                relation_types=relation_types,
                limit=self.max_nodes,
            )
        }
        return sorted(left & right)
