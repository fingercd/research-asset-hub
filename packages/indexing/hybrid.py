"""API-facing hybrid keyword/vector search orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .embeddings import Embedder, EmbeddingStatus
from .fusion import reciprocal_rank_fusion
from .meili import KeywordIndex
from .models import BackendHealth, BackendState, SearchHit
from .qdrant import VectorIndex
from .rerank import Reranker, RerankerStatus


class SearchMode(StrEnum):
    EXACT = "exact"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    SEMANTIC = "semantic"


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    hits: tuple[SearchHit, ...]
    mode: SearchMode
    degraded: bool
    keyword_health: tuple[BackendHealth, ...]
    vector_health: tuple[BackendHealth, ...]
    embedding_status: EmbeddingStatus | None
    reranker_status: RerankerStatus | None
    errors: tuple[str, ...] = ()


class HybridSearchService:
    """Fuse each backend's top 100 with RRF ``k=60`` and exact-term protection."""

    def __init__(
        self,
        keyword_indexes: Mapping[str, KeywordIndex],
        *,
        asset_vectors: VectorIndex | None = None,
        chunk_vectors: VectorIndex | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        candidate_limit: int = 100,
        rerank_limit: int = 30,
    ) -> None:
        self.keyword_indexes = dict(keyword_indexes)
        self.vector_indexes = tuple(
            index for index in (asset_vectors, chunk_vectors) if index is not None
        )
        self.embedder = embedder
        self.reranker = reranker
        self.candidate_limit = candidate_limit
        self.rerank_limit = rerank_limit

    def search(
        self,
        query: str,
        *,
        mode: SearchMode | str = SearchMode.HYBRID,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        candidate_limit: int | None = None,
        rerank: bool = True,
    ) -> HybridSearchResult:
        selected_mode = SearchMode(mode)
        search_window = max(
            1,
            self.candidate_limit if candidate_limit is None else candidate_limit,
        )
        requested_limit = min(max(1, limit), search_window)
        errors: list[str] = []
        keyword_sets: list[Sequence[SearchHit]] = []
        vector_sets: list[Sequence[SearchHit]] = []

        if selected_mode != SearchMode.SEMANTIC or self.keyword_indexes:
            keyword_items = list(self.keyword_indexes.items())
            asset_item = next(
                (item for item in keyword_items if item[0] == "assets"),
                None,
            )
            if asset_item is not None:
                keyword_items.remove(asset_item)
                try:
                    asset_hits = asset_item[1].search(
                        query,
                        limit=search_window,
                        filters=filters,
                    )
                    keyword_sets.append(asset_hits)
                except Exception as error:
                    errors.append(f"keyword:{asset_item[0]}:{type(error).__name__}")
                    asset_hits = []
                exact_asset_found = any(
                    hit.exact_match and hit.document.kind == "asset"
                    for hit in asset_hits
                )
                if (
                    exact_asset_found
                    and selected_mode in {SearchMode.EXACT, SearchMode.KEYWORD}
                ):
                    keyword_items = []
            for name, index in keyword_items:
                try:
                    keyword_sets.append(
                        index.search(
                            query,
                            limit=search_window,
                            filters=filters,
                        )
                    )
                except Exception as error:
                    errors.append(f"keyword:{name}:{type(error).__name__}")
        keyword_hits = reciprocal_rank_fusion(
            keyword_sets,
            k=60,
            exact_boost=0.25,
            limit=search_window,
            group_by_asset=True,
        )

        embedding_status: EmbeddingStatus | None = None
        needs_vectors = selected_mode in {SearchMode.HYBRID, SearchMode.SEMANTIC}
        if needs_vectors and query.strip() and self.embedder is not None:
            try:
                vector = self.embedder.encode([query])[0]
                embedding_status = self.embedder.status
                for index_number, vector_index in enumerate(self.vector_indexes):
                    try:
                        vector_sets.append(
                            vector_index.search(
                                vector,
                                limit=search_window,
                                filters=filters,
                            )
                        )
                    except Exception as error:
                        errors.append(f"vector:{index_number}:{type(error).__name__}")
            except Exception as error:
                errors.append(f"embedding:{type(error).__name__}")
                embedding_status = self.embedder.status
        vector_hits = reciprocal_rank_fusion(
            vector_sets,
            k=60,
            exact_boost=0.0,
            limit=search_window,
            group_by_asset=True,
        )

        if selected_mode == SearchMode.EXACT:
            exact_hits = [hit for hit in keyword_hits if hit.exact_match]
            fused = exact_hits or keyword_hits
        elif selected_mode == SearchMode.KEYWORD:
            fused = keyword_hits
        elif selected_mode == SearchMode.SEMANTIC:
            protected = [hit for hit in keyword_hits if hit.exact_match]
            fused = reciprocal_rank_fusion(
                [protected, vector_hits],
                k=60,
                exact_boost=0.25,
                limit=search_window,
                group_by_asset=True,
            )
        else:
            fused = reciprocal_rank_fusion(
                [keyword_hits, vector_hits],
                k=60,
                exact_boost=0.25,
                limit=search_window,
                group_by_asset=True,
            )

        fused = _deduplicate_assets(fused)
        reranker_status: RerankerStatus | None = None
        if (
            rerank
            and selected_mode == SearchMode.HYBRID
            and self.reranker is not None
            and fused
        ):
            rerank_count = min(self.rerank_limit, len(fused))
            try:
                reranked = self.reranker.rerank(
                    query,
                    fused[:rerank_count],
                    limit=rerank_count,
                )
                reranker_status = self.reranker.status
                fused = [
                    *reranked,
                    *[
                        replace(hit, rank=rank)
                        for rank, hit in enumerate(
                            fused[rerank_count:],
                            start=rerank_count + 1,
                        )
                    ],
                ]
            except Exception as error:
                errors.append(f"reranker:{type(error).__name__}")
                reranker_status = self.reranker.status

        fused.sort(
            key=lambda hit: (
                not (hit.exact_match and hit.document.kind == "asset"),
                not hit.exact_match,
                hit.rank,
                hit.document.document_id,
            )
        )

        keyword_health = tuple(index.health for index in self.keyword_indexes.values())
        vector_health = tuple(index.health for index in self.vector_indexes)
        degraded = bool(errors) or any(
            health.state != BackendState.HEALTHY for health in (*keyword_health, *vector_health)
        )
        if embedding_status is not None:
            degraded = degraded or embedding_status.provider == "local-hashing"
        if reranker_status is not None:
            degraded = degraded or reranker_status.provider == "local-lexical"
        return HybridSearchResult(
            hits=tuple(
                replace(hit, rank=rank) for rank, hit in enumerate(fused[:requested_limit], start=1)
            ),
            mode=selected_mode,
            degraded=degraded,
            keyword_health=keyword_health,
            vector_health=vector_health,
            embedding_status=embedding_status,
            reranker_status=reranker_status,
            errors=tuple(errors),
        )


def _deduplicate_assets(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Keep one representative per asset before expensive reranking."""

    selected: list[SearchHit] = []
    positions: dict[str, int] = {}
    for hit in hits:
        existing_position = positions.get(hit.document.asset_id)
        if existing_position is None:
            positions[hit.document.asset_id] = len(selected)
            selected.append(hit)
            continue
        existing = selected[existing_position]
        if (
            hit.exact_match
            and hit.document.kind == "asset"
            and not (
                existing.exact_match
                and existing.document.kind == "asset"
            )
        ):
            selected[existing_position] = hit
    return selected
