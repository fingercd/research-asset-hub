"""Rank fusion for keyword, vector and exact-match candidates."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .models import SearchDocument, SearchHit


def reciprocal_rank_fusion(
    result_sets: Sequence[Sequence[SearchHit]],
    *,
    k: int = 60,
    exact_boost: float = 0.25,
    limit: int = 20,
    group_by_asset: bool = False,
) -> list[SearchHit]:
    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores: dict[str, float] = {}
    exact: dict[str, bool] = {}
    documents: dict[str, SearchDocument] = {}
    representative_ranks: dict[str, int] = {}
    sources: dict[str, set[str]] = {}
    for result_set in result_sets:
        seen_in_result_set: set[str] = set()
        for fallback_rank, hit in enumerate(result_set, start=1):
            rank = hit.rank if hit.rank > 0 else fallback_rank
            key = (
                hit.document.asset_id
                if group_by_asset
                else hit.document.document_id
            )
            if key in seen_in_result_set:
                continue
            seen_in_result_set.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            exact[key] = exact.get(key, False) or hit.exact_match
            existing = documents.get(key)
            existing_is_exact_asset = bool(
                existing is not None
                and exact.get(key, False)
                and existing.kind == "asset"
            )
            current_is_exact_asset = hit.exact_match and hit.document.kind == "asset"
            if (
                existing is None
                or current_is_exact_asset
                or (
                    not existing_is_exact_asset
                    and rank < representative_ranks[key]
                )
            ):
                documents[key] = hit.document
                representative_ranks[key] = rank
            sources.setdefault(key, set()).add(hit.source)
    for key, is_exact in exact.items():
        if is_exact:
            scores[key] += exact_boost
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    return [
        SearchHit(
            document=documents[key],
            score=scores[key],
            rank=rank,
            source="+".join(sorted(sources[key])),
            exact_match=exact[key],
        )
        for rank, key in enumerate(ordered[:limit], start=1)
    ]


def apply_score_boosts(
    hits: Sequence[SearchHit],
    boosts: dict[str, float],
) -> list[SearchHit]:
    rescored = [
        replace(
            hit,
            score=hit.score + boosts.get(hit.document.document_id, 0.0),
        )
        for hit in hits
    ]
    rescored.sort(key=lambda hit: (-hit.score, hit.document.document_id))
    return [replace(hit, rank=rank) for rank, hit in enumerate(rescored, start=1)]
