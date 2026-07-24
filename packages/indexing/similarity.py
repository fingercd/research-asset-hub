"""Hybrid similar-paper ranking."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .fusion import apply_score_boosts, reciprocal_rank_fusion
from .meili import KeywordIndex
from .models import SearchHit
from .qdrant import VectorIndex


class SimilarPaperFinder:
    def __init__(self, keyword: KeywordIndex, vectors: VectorIndex) -> None:
        self.keyword = keyword
        self.vectors = vectors

    def find(
        self,
        *,
        asset_id: str,
        title: str,
        vector: Sequence[float],
        limit: int = 10,
        filters: Mapping[str, object] | None = None,
        related_document_boosts: Mapping[str, float] | None = None,
    ) -> list[SearchHit]:
        expanded_limit = max(limit * 3, 20)
        keyword_hits = self.keyword.search(
            title,
            limit=expanded_limit,
            filters=filters,
        )
        vector_hits = self.vectors.search(
            vector,
            limit=expanded_limit,
            filters=filters,
        )
        fused = reciprocal_rank_fusion(
            [keyword_hits, vector_hits],
            limit=expanded_limit,
        )
        filtered = [hit for hit in fused if hit.document.asset_id != asset_id]
        boosted = apply_score_boosts(
            filtered,
            dict(related_document_boosts or {}),
        )
        return boosted[:limit]
