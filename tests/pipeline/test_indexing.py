from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from packages.indexing import (
    BackendHealth,
    BackendState,
    InMemoryKeywordIndex,
    ResilientKeywordIndex,
    SearchDocument,
    SearchHit,
    reciprocal_rank_fusion,
)


def document(
    document_id: str,
    title: str,
    text: str,
    *,
    asset_id: str | None = None,
    domain: str = "agent",
) -> SearchDocument:
    return SearchDocument(
        document_id=document_id,
        asset_id=asset_id or document_id,
        kind="paper",
        title=title,
        text=text,
        metadata={"domain": domain, "kind": "paper"},
        exact_terms=(document_id,),
    )


class FailingKeywordIndex:
    @property
    def health(self) -> BackendHealth:
        return BackendHealth("failed", BackendState.UNAVAILABLE)

    def upsert(self, documents: Sequence[SearchDocument]) -> None:
        raise OSError("offline")

    def delete(self, document_ids: Sequence[str]) -> None:
        raise OSError("offline")

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        raise OSError("offline")


def test_keyword_exact_boost_and_facets() -> None:
    index = InMemoryKeywordIndex()
    index.upsert(
        [
            document("10.1/exact", "A Paper", "generic text"),
            document("other", "Exact topic", "10.1/exact appears once", domain="infra"),
        ]
    )
    hits = index.search("10.1/exact")
    assert hits[0].document.document_id == "10.1/exact"
    assert hits[0].exact_match is True
    assert index.search("exact", filters={"domain": "infra"})[0].document.asset_id == "other"


def test_resilient_keyword_index_falls_back_without_service() -> None:
    index = ResilientKeywordIndex(FailingKeywordIndex())
    index.upsert([document("d1", "Fallback Search", "works offline")])
    assert index.health.state == BackendState.DEGRADED
    assert index.search("offline")[0].document.document_id == "d1"


def test_rrf_groups_asset_evidence_without_chunk_count_inflation() -> None:
    first_chunk = SearchHit(
        document=document("chunk-1", "Paper", "evidence", asset_id="paper"),
        score=1.0,
        rank=1,
        source="chunks",
    )
    duplicate_chunk = SearchHit(
        document=document("chunk-2", "Paper", "more evidence", asset_id="paper"),
        score=0.9,
        rank=2,
        source="chunks",
    )
    asset_hit = SearchHit(
        document=document("asset-paper", "Paper", "summary", asset_id="paper"),
        score=0.8,
        rank=3,
        source="assets",
    )

    fused = reciprocal_rank_fusion(
        [[first_chunk, duplicate_chunk], [asset_hit]],
        k=60,
        group_by_asset=True,
    )

    assert len(fused) == 1
    assert fused[0].document.asset_id == "paper"
    assert fused[0].score == pytest.approx((1 / 61) + (1 / 63))
