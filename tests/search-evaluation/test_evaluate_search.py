from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.indexing import SearchDocument, SearchHit, SearchMode
from scripts.evaluate_search import (
    SearchEvaluationQuery,
    build_index_scope,
    deduplicate_asset_ids,
    evaluate_rankings,
    load_queries,
    ndcg_at_k,
    percentile_95,
    recall_at_k,
    select_queries_for_index_scope,
)


def _hit(asset_id: str, rank: int) -> SearchHit:
    return SearchHit(
        document=SearchDocument(
            document_id=f"chunk:{rank}",
            asset_id=asset_id,
            kind="document",
            title=asset_id,
            text="text",
        ),
        score=1 / rank,
        rank=rank,
        source="test",
    )


def test_fixed_search_fixture_covers_locked_exact_categories() -> None:
    fixture = Path(__file__).with_name("queries.yaml")
    queries = load_queries(fixture)
    assert len(queries) == 12
    assert {item.category for item in queries if item.exact} >= {
        "doi",
        "arxiv",
        "title",
        "code_symbol",
    }
    assert {item.mode for item in queries} >= {
        SearchMode.EXACT,
        SearchMode.KEYWORD,
    }


def test_code_query_is_explicitly_skipped_when_code_index_is_disabled() -> None:
    fixture = Path(__file__).with_name("queries.yaml")
    queries = load_queries(fixture)

    selected, skipped = select_queries_for_index_scope(
        queries,
        code_index_enabled=False,
    )

    assert len(selected) == len(queries) - 1
    assert all(item.category != "code_symbol" for item in selected)
    assert skipped == (
        {
            "id": "exact-code-symbol",
            "category": "code_symbol",
            "reason": "code_index_disabled",
        },
    )
    assert build_index_scope(
        code_index_enabled=False,
        fixture_query_count=len(queries),
        evaluated_query_count=len(selected),
        skipped_query_count=len(skipped),
    ) == {
        "code_index_enabled": False,
        "selected_indexes": ["assets", "documents", "annotations"],
        "skipped_indexes": ["code"],
        "fixture_query_count": 12,
        "evaluated_query_count": 11,
        "skipped_query_count": 1,
    }


def test_code_query_remains_in_scope_when_code_index_is_enabled() -> None:
    fixture = Path(__file__).with_name("queries.yaml")
    queries = load_queries(fixture)

    selected, skipped = select_queries_for_index_scope(
        queries,
        code_index_enabled=True,
    )

    assert selected == queries
    assert skipped == ()
    assert build_index_scope(
        code_index_enabled=True,
        fixture_query_count=len(queries),
        evaluated_query_count=len(selected),
        skipped_query_count=len(skipped),
    )["selected_indexes"] == ["assets", "documents", "code", "annotations"]


def test_asset_level_deduplication_prevents_chunk_heavy_assets_from_inflating_rank() -> None:
    hits = [
        _hit("asset-a", 1),
        _hit("asset-a", 2),
        _hit("asset-b", 3),
        _hit("asset-c", 4),
    ]
    assert deduplicate_asset_ids(hits, limit=2) == ("asset-a", "asset-b")


def test_recall_ndcg_and_aggregate_metrics_use_fixed_relevance() -> None:
    query = SearchEvaluationQuery(
        query_id="q1",
        query="query",
        mode=SearchMode.KEYWORD,
        category="concept",
        relevance={"asset-a": 3.0, "asset-b": 1.0},
        exact=True,
    )
    ranking = ("asset-b", "noise", "asset-a")
    assert recall_at_k(ranking, query.relevance, k=2) == 0.5
    assert recall_at_k(ranking, query.relevance, k=3) == 1.0
    assert 0 < ndcg_at_k(ranking, query.relevance, k=3) < 1
    report = evaluate_rankings((query,), {"q1": ranking})
    assert report["exact_recall_at_5"] == 1.0
    assert report["recall_at_10"] == 1.0
    assert 0 < report["ndcg_at_10"] < 1


def test_nearest_rank_p95_is_deterministic() -> None:
    assert percentile_95([float(index) for index in range(1, 21)]) == 19.0
    with pytest.raises(ValueError):
        percentile_95([])
