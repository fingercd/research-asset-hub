"""Hybrid keyword/vector evidence retrieval."""

from __future__ import annotations

from packages.indexing.fusion import reciprocal_rank_fusion
from packages.indexing.meili import KeywordIndex
from packages.indexing.qdrant import VectorIndex

from .models import RetrievalRequest, RetrievedEvidence


class HybridRetriever:
    def __init__(
        self,
        keyword: KeywordIndex,
        vectors: VectorIndex | None = None,
    ) -> None:
        self.keyword = keyword
        self.vectors = vectors

    def retrieve(self, request: RetrievalRequest) -> list[RetrievedEvidence]:
        expanded_limit = max(20, request.limit * 3)
        keyword_hits = self.keyword.search(
            request.query,
            limit=expanded_limit,
            filters=request.filters,
        )
        result_sets = [keyword_hits]
        if request.vector is not None and self.vectors is not None:
            result_sets.append(
                self.vectors.search(
                    request.vector,
                    limit=expanded_limit,
                    filters=request.filters,
                )
            )
        fused = reciprocal_rank_fusion(result_sets, limit=request.limit)
        return [
            RetrievedEvidence(
                hit=hit,
                citation_id=hit.document.document_id,
            )
            for hit in fused
        ]
