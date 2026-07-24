from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from packages.indexing.models import SearchDocument, VectorPoint
from packages.indexing.qdrant import QdrantIndex


class _SizeLimitedTransport:
    def __init__(self, *, timeout: bool = False) -> None:
        self.batch_sizes: list[int] = []
        self.accepted_ids: list[str] = []
        self.accepted_text_lengths: list[int] = []
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        points = list((payload or {}).get("points", []))
        self.batch_sizes.append(len(points))
        if len(points) > 1:
            request = httpx.Request(method, f"http://qdrant.test{path}")
            if self.timeout:
                raise httpx.WriteTimeout("request too large", request=request)
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError(
                "request too large",
                request=request,
                response=response,
            )
        self.accepted_ids.extend(str(point["id"]) for point in points)
        self.accepted_text_lengths.extend(
            len(str(point["payload"]["text"])) for point in points
        )
        return {"status": "ok"}


def _point(index: int) -> VectorPoint:
    document = SearchDocument(
        document_id=f"document-{index}",
        asset_id="asset",
        kind="document",
        title="Title",
        text="text",
    )
    return VectorPoint(
        point_id=f"00000000-0000-0000-0000-{index:012d}",
        document=document,
        vector=(1.0, 0.0),
    )


def test_qdrant_upsert_splits_size_rejected_batches() -> None:
    transport = _SizeLimitedTransport()
    index = QdrantIndex(
        transport,
        collection_name="chunks",
        dimension=2,
    )

    index.upsert([_point(1), _point(2), _point(3)])

    assert transport.batch_sizes == [3, 1, 2, 1, 1]
    assert transport.accepted_ids == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]


def test_qdrant_upsert_splits_timed_out_batches() -> None:
    transport = _SizeLimitedTransport(timeout=True)
    index = QdrantIndex(
        transport,
        collection_name="chunks",
        dimension=2,
    )

    index.upsert([_point(1), _point(2)])

    assert transport.batch_sizes == [2, 1, 1]
    assert len(transport.accepted_ids) == 2


def test_qdrant_payload_bounds_large_chunk_text() -> None:
    transport = _SizeLimitedTransport()
    index = QdrantIndex(
        transport,
        collection_name="chunks",
        dimension=2,
    )
    point = _point(1)
    point = VectorPoint(
        point_id=point.point_id,
        document=SearchDocument(
            document_id=point.document.document_id,
            asset_id=point.document.asset_id,
            kind=point.document.kind,
            title=point.document.title,
            text="x" * 100_000,
        ),
        vector=point.vector,
    )

    index.upsert([point])

    assert transport.accepted_text_lengths == [32_768]
