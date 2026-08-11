"""Keyword-only Meilisearch search orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .fusion import reciprocal_rank_fusion
from .meili import KeywordIndex
from .models import BackendHealth, BackendState, SearchHit


class SearchMode(StrEnum):
    EXACT = "exact"
    KEYWORD = "keyword"


@dataclass(frozen=True, slots=True)
class KeywordSearchResult:
    hits: tuple[SearchHit, ...]
    mode: SearchMode
    degraded: bool
    keyword_health: tuple[BackendHealth, ...]
    errors: tuple[str, ...] = ()


class KeywordSearchService:
    """Fuse the four Meilisearch indexes without any local model or vector backend."""

    def __init__(
        self,
        keyword_indexes: Mapping[str, KeywordIndex],
        *,
        candidate_limit: int = 100,
    ) -> None:
        self.keyword_indexes = dict(keyword_indexes)
        self.candidate_limit = candidate_limit

    def search(
        self,
        query: str,
        *,
        mode: SearchMode | str = SearchMode.KEYWORD,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        candidate_limit: int | None = None,
    ) -> KeywordSearchResult:
        selected_mode = SearchMode(mode)
        search_window = max(
            1,
            self.candidate_limit if candidate_limit is None else candidate_limit,
        )
        requested_limit = min(max(1, limit), search_window)
        errors: list[str] = []
        keyword_sets: list[Sequence[SearchHit]] = []

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
            if exact_asset_found:
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
        if selected_mode == SearchMode.EXACT:
            exact_hits = [hit for hit in keyword_hits if hit.exact_match]
            fused = exact_hits or keyword_hits
        else:
            fused = keyword_hits
        fused = _deduplicate_assets(fused)
        fused.sort(
            key=lambda hit: (
                not (hit.exact_match and hit.document.kind == "asset"),
                not hit.exact_match,
                hit.rank,
                hit.document.document_id,
            )
        )
        keyword_health = tuple(index.health for index in self.keyword_indexes.values())
        degraded = bool(errors) or any(
            health.state != BackendState.HEALTHY for health in keyword_health
        )
        return KeywordSearchResult(
            hits=tuple(
                replace(hit, rank=rank)
                for rank, hit in enumerate(fused[:requested_limit], start=1)
            ),
            mode=selected_mode,
            degraded=degraded,
            keyword_health=keyword_health,
            errors=tuple(errors),
        )


def _deduplicate_assets(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Keep one representative per asset before returning ranked results."""

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
