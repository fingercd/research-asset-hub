"""Qdrant abstraction and cosine-similarity memory backend."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx

from .filters import qdrant_filter
from .meili import JSONTransport, _matches_filters
from .models import BackendHealth, BackendState, SearchDocument, SearchHit, VectorPoint

MAX_VECTOR_PAYLOAD_TEXT_CHARS = 32_768


class VectorIndex(Protocol):
    @property
    def health(self) -> BackendHealth: ...

    def upsert(self, points: Sequence[VectorPoint]) -> None: ...

    def delete(self, point_ids: Sequence[str]) -> None: ...

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]: ...


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Vector dimensions differ")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class InMemoryVectorIndex:
    def __init__(self) -> None:
        self.points: dict[str, VectorPoint] = {}
        self.dimension: int | None = None

    @property
    def health(self) -> BackendHealth:
        return BackendHealth("memory-vector", BackendState.HEALTHY)

    def upsert(self, points: Sequence[VectorPoint]) -> None:
        for point in points:
            if self.dimension is None:
                self.dimension = len(point.vector)
            if len(point.vector) != self.dimension:
                raise ValueError("Cannot mix vector dimensions")
            self.points[point.point_id] = point

    def delete(self, point_ids: Sequence[str]) -> None:
        for point_id in point_ids:
            self.points.pop(point_id, None)

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        if self.dimension is not None and len(vector) != self.dimension:
            raise ValueError("Query vector dimension differs from the index")
        scored = [
            (cosine_similarity(vector, point.vector), point)
            for point in self.points.values()
            if not filters or _matches_filters(point.document, filters)
        ]
        scored.sort(key=lambda item: (-item[0], item[1].point_id))
        return [
            SearchHit(
                document=point.document,
                score=score,
                rank=rank,
                source="memory-vector",
            )
            for rank, (score, point) in enumerate(scored[:limit], start=1)
        ]


class QdrantIndex:
    def __init__(
        self,
        transport: JSONTransport,
        *,
        collection_name: str,
        dimension: int,
    ) -> None:
        self.transport = transport
        self.collection_name = collection_name
        self.dimension = dimension

    @property
    def health(self) -> BackendHealth:
        try:
            self.transport.request("GET", f"/collections/{self.collection_name}")
        except Exception as exc:
            return BackendHealth("qdrant", BackendState.UNAVAILABLE, str(exc))
        return BackendHealth("qdrant", BackendState.HEALTHY)

    def upsert(self, points: Sequence[VectorPoint]) -> None:
        for point in points:
            if len(point.vector) != self.dimension:
                raise ValueError("Point vector dimension differs from collection")
            if not all(math.isfinite(value) for value in point.vector):
                raise ValueError("Point vector contains a non-finite value")
        self._upsert_with_split(points)

    def _upsert_with_split(self, points: Sequence[VectorPoint]) -> None:
        try:
            self.transport.request(
                "PUT",
                f"/collections/{self.collection_name}/points",
                payload={
                    "points": [
                        {
                            "id": point.point_id,
                            "vector": list(point.vector),
                            "payload": {
                                "document_id": point.document.document_id,
                                "asset_id": point.document.asset_id,
                                "kind": point.document.kind,
                                "title": point.document.title,
                                "text": point.document.text[
                                    :MAX_VECTOR_PAYLOAD_TEXT_CHARS
                                ],
                                "metadata": point.document.metadata,
                                "exact_terms": list(point.document.exact_terms),
                            },
                        }
                        for point in points
                    ]
                },
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException) as error:
            retryable = isinstance(error, httpx.TimeoutException) or (
                error.response.status_code in {400, 413}
            )
            if not retryable or len(points) <= 1:
                raise
            midpoint = len(points) // 2
            self._upsert_with_split(points[:midpoint])
            self._upsert_with_split(points[midpoint:])

    def delete(self, point_ids: Sequence[str]) -> None:
        self.transport.request(
            "POST",
            f"/collections/{self.collection_name}/points/delete",
            payload={"points": list(point_ids)},
        )

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        if len(vector) != self.dimension:
            raise ValueError("Query vector dimension differs from collection")
        response = self.transport.request(
            "POST",
            f"/collections/{self.collection_name}/points/search",
            payload={
                "vector": list(vector),
                "limit": limit,
                "filter": qdrant_filter(filters),
                "with_payload": True,
            },
        )
        values = response.get("result", [])
        if not isinstance(values, Sequence):
            return []
        hits: list[SearchHit] = []
        for rank, value in enumerate(values, start=1):
            if not isinstance(value, Mapping):
                continue
            payload = value.get("payload")
            if not isinstance(payload, Mapping):
                continue
            document = SearchDocument(
                document_id=str(payload.get("document_id", value.get("id", ""))),
                asset_id=str(payload.get("asset_id", "")),
                kind=str(payload.get("kind", "document")),
                title=str(payload.get("title", "")),
                text=str(payload.get("text", "")),
                metadata=(
                    dict(payload["metadata"])
                    if isinstance(payload.get("metadata"), Mapping)
                    else {}
                ),
                exact_terms=tuple(str(item) for item in payload.get("exact_terms", []) or []),
            )
            hits.append(
                SearchHit(
                    document=document,
                    score=float(value.get("score", 0.0)),
                    rank=rank,
                    source="qdrant",
                )
            )
        return hits


class QdrantAdmin:
    """Create versioned cosine collections and their filter payload indexes."""

    FILTER_FIELDS: Mapping[str, str] = {
        "metadata.domains": "keyword",
        "metadata.year": "integer",
        "metadata.asset_type": "keyword",
        "metadata.reading_status": "keyword",
        "metadata.tags": "keyword",
        "metadata.has_pdf": "bool",
        "metadata.has_html": "bool",
        "metadata.has_code": "bool",
        "metadata.has_guide": "bool",
        "metadata.representation_types": "keyword",
        "metadata.available_representation_types": "keyword",
        "metadata.availability": "keyword",
    }

    def __init__(self, transport: JSONTransport) -> None:
        self.transport = transport

    def ensure_collection(self, collection_name: str, *, dimension: int) -> None:
        try:
            response = self.transport.request("GET", f"/collections/{collection_name}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            self.transport.request(
                "PUT",
                f"/collections/{collection_name}",
                payload={
                    "vectors": {
                        "size": dimension,
                        "distance": "Cosine",
                    },
                    "on_disk_payload": True,
                },
            )
        else:
            existing = _collection_dimension(response)
            if existing is not None and existing != dimension:
                raise ValueError(
                    f"Qdrant collection {collection_name!r} has dimension "
                    f"{existing}; expected {dimension}"
                )
        for field_name, field_schema in self.FILTER_FIELDS.items():
            self.transport.request(
                "PUT",
                f"/collections/{collection_name}/index",
                payload={
                    "field_name": field_name,
                    "field_schema": field_schema,
                },
            )

    def point_count(self, collection_name: str) -> int:
        response = self.transport.request("GET", f"/collections/{collection_name}")
        result = response.get("result")
        if not isinstance(result, Mapping):
            return 0
        return int(result.get("points_count", 0))

    def point_ids(self, collection_name: str) -> set[str]:
        values: set[str] = set()
        offset: str | int | None = None
        while True:
            payload: dict[str, Any] = {
                "limit": 256,
                "with_payload": False,
                "with_vector": False,
            }
            if offset is not None:
                payload["offset"] = offset
            response = self.transport.request(
                "POST",
                f"/collections/{collection_name}/points/scroll",
                payload=payload,
            )
            result = response.get("result")
            if not isinstance(result, Mapping):
                break
            points = result.get("points")
            if not isinstance(points, Sequence):
                break
            for point in points:
                if isinstance(point, Mapping) and point.get("id") is not None:
                    values.add(str(point["id"]))
            next_offset = result.get("next_page_offset")
            if next_offset is None or not points:
                break
            offset = str(next_offset) if isinstance(next_offset, str) else int(next_offset)
        return values


def _collection_dimension(response: Mapping[str, Any]) -> int | None:
    result = response.get("result")
    if not isinstance(result, Mapping):
        return None
    config = result.get("config")
    if not isinstance(config, Mapping):
        return None
    params = config.get("params")
    if not isinstance(params, Mapping):
        return None
    vectors = params.get("vectors")
    if not isinstance(vectors, Mapping):
        return None
    size = vectors.get("size")
    return int(size) if isinstance(size, int) else None


class ResilientVectorIndex:
    def __init__(
        self,
        primary: VectorIndex,
        fallback: InMemoryVectorIndex | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or InMemoryVectorIndex()
        self.last_error: str | None = None

    @property
    def health(self) -> BackendHealth:
        if self.last_error is None and self.primary.health.state == BackendState.HEALTHY:
            return self.primary.health
        return BackendHealth("vector-resilient", BackendState.DEGRADED, self.last_error)

    def upsert(self, points: Sequence[VectorPoint]) -> None:
        self.fallback.upsert(points)
        try:
            self.primary.upsert(points)
        except Exception as exc:
            self.last_error = str(exc)

    def delete(self, point_ids: Sequence[str]) -> None:
        self.fallback.delete(point_ids)
        try:
            self.primary.delete(point_ids)
        except Exception as exc:
            self.last_error = str(exc)

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        try:
            hits = self.primary.search(vector, limit=limit, filters=filters)
        except Exception as exc:
            self.last_error = str(exc)
            return self.fallback.search(vector, limit=limit, filters=filters)
        self.last_error = None
        return hits
