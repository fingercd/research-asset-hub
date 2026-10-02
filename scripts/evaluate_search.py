"""Evaluate the production Meilisearch keyword indexes against a fixed truth set.

The evaluator reports asset-level metrics.  Multiple matching chunks from the
same asset are collapsed before scoring so repository-heavy papers cannot
inflate recall or nDCG.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.api.search_runtime import SearchRuntime
from packages.domain.models import Asset
from packages.indexing import KEYWORD_INDEX_NAMES, SearchHit, SearchMode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRUTH_PATH = PROJECT_ROOT / "tests" / "search-evaluation" / "queries.yaml"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "work" / "validation" / "search-evaluation.json"
CODE_QUERY_CATEGORY = "code_symbol"


@dataclass(frozen=True, slots=True)
class SearchEvaluationQuery:
    query_id: str
    query: str
    mode: SearchMode
    category: str
    relevance: Mapping[str, float]
    exact: bool = False


@dataclass(frozen=True, slots=True)
class SearchThresholds:
    exact_recall_at_5: float = 1.0
    recall_at_10: float = 0.85
    ndcg_at_10: float = 0.75
    structured_p95_ms: float = 300.0
    keyword_p95_ms: float = 800.0


def select_queries_for_index_scope(
    queries: Sequence[SearchEvaluationQuery],
    *,
    code_index_enabled: bool,
) -> tuple[tuple[SearchEvaluationQuery, ...], tuple[dict[str, str], ...]]:
    """Select queries supported by the configured serving indexes.

    The fixed fixture intentionally keeps a ``code_symbol`` query so that a
    code-enabled run continues to exercise that index.  When the production
    code index is disabled, those queries are skipped rather than counted as
    failed retrievals.  Returning a structured skip record keeps the omission
    visible in the validation report and gives callers a deterministic reason.
    """

    selected: list[SearchEvaluationQuery] = []
    skipped: list[dict[str, str]] = []
    for query in queries:
        if not code_index_enabled and query.category == CODE_QUERY_CATEGORY:
            skipped.append(
                {
                    "id": query.query_id,
                    "category": query.category,
                    "reason": "code_index_disabled",
                }
            )
            continue
        selected.append(query)
    return tuple(selected), tuple(skipped)


def build_index_scope(
    *,
    code_index_enabled: bool,
    fixture_query_count: int,
    evaluated_query_count: int,
    skipped_query_count: int,
) -> dict[str, Any]:
    """Describe the keyword indexes and fixed queries used for evaluation."""

    selected_indexes = tuple(
        name
        for name in KEYWORD_INDEX_NAMES
        if code_index_enabled or name != "code"
    )
    skipped_indexes = tuple(
        name for name in KEYWORD_INDEX_NAMES if name not in selected_indexes
    )
    return {
        "code_index_enabled": code_index_enabled,
        "selected_indexes": list(selected_indexes),
        "skipped_indexes": list(skipped_indexes),
        "fixture_query_count": fixture_query_count,
        "evaluated_query_count": evaluated_query_count,
        "skipped_query_count": skipped_query_count,
    }


def _mapping(value: object, *, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be a mapping")
    return {str(key): item for key, item in value.items()}


def load_queries(path: Path) -> tuple[SearchEvaluationQuery, ...]:
    """Load and strictly validate the versioned fixed-query fixture."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = _mapping(document, location="root")
    if root.get("schema_version") != 2:
        raise ValueError("search fixture schema_version must be 2")
    raw_queries = root.get("queries")
    if not isinstance(raw_queries, Sequence) or isinstance(raw_queries, (str, bytes)):
        raise ValueError("queries must be a sequence")
    queries: list[SearchEvaluationQuery] = []
    for index, raw_query in enumerate(raw_queries):
        item = _mapping(raw_query, location=f"queries[{index}]")
        query_id = str(item.get("id") or "").strip()
        query = str(item.get("query") or "").strip()
        category = str(item.get("category") or "").strip()
        if not query_id or not query or not category:
            raise ValueError(f"queries[{index}] requires id, query, and category")
        try:
            mode = SearchMode(str(item.get("mode") or "keyword"))
        except ValueError as error:
            raise ValueError(f"queries[{index}].mode is invalid") from error
        raw_relevance = _mapping(
            item.get("relevant_assets"),
            location=f"queries[{index}].relevant_assets",
        )
        relevance: dict[str, float] = {}
        for raw_asset_id, raw_grade in raw_relevance.items():
            try:
                asset_id = str(UUID(raw_asset_id))
                if not isinstance(raw_grade, (int, float, str)):
                    raise TypeError
                grade = float(raw_grade)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"queries[{index}].relevant_assets has an invalid entry"
                ) from error
            if grade <= 0:
                raise ValueError("relevance grades must be positive")
            relevance[asset_id] = grade
        if not relevance:
            raise ValueError(f"queries[{index}] needs at least one relevant asset")
        queries.append(
            SearchEvaluationQuery(
                query_id=query_id,
                query=query,
                mode=mode,
                category=category,
                relevance=relevance,
                exact=bool(item.get("exact", False)),
            )
        )
    if not queries or len({item.query_id for item in queries}) != len(queries):
        raise ValueError("search fixture query IDs must be non-empty and unique")
    exact_categories = {item.category for item in queries if item.exact}
    required_exact = {"doi", "arxiv", "title", "code_symbol"}
    missing = sorted(required_exact - exact_categories)
    if missing:
        raise ValueError(f"fixed exact-query categories are missing: {', '.join(missing)}")
    return tuple(queries)


def deduplicate_asset_ids(hits: Sequence[SearchHit], *, limit: int) -> tuple[str, ...]:
    """Return first-seen asset IDs, matching the API's asset-level projection."""

    values: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        asset_id = hit.document.asset_id
        if asset_id in seen:
            continue
        seen.add(asset_id)
        values.append(asset_id)
        if len(values) >= limit:
            break
    return tuple(values)


def recall_at_k(
    ranked_asset_ids: Sequence[str],
    relevance: Mapping[str, float],
    *,
    k: int,
) -> float:
    relevant = set(relevance)
    if not relevant:
        return 1.0
    return len(relevant.intersection(ranked_asset_ids[:k])) / len(relevant)


def ndcg_at_k(
    ranked_asset_ids: Sequence[str],
    relevance: Mapping[str, float],
    *,
    k: int,
) -> float:
    def dcg(grades: Sequence[float]) -> float:
        return sum(
            (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1)
        )

    observed = [relevance.get(asset_id, 0.0) for asset_id in ranked_asset_ids[:k]]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    ideal_score = dcg(ideal)
    return dcg(observed) / ideal_score if ideal_score else 1.0


def percentile_95(values: Sequence[float]) -> float:
    """Nearest-rank P95, suitable for deterministic release-gate reporting."""

    if not values:
        raise ValueError("cannot calculate P95 of an empty sequence")
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def evaluate_rankings(
    queries: Sequence[SearchEvaluationQuery],
    rankings: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Calculate exact and comprehensive asset-level retrieval metrics."""

    rows: list[dict[str, Any]] = []
    for query in queries:
        ranked = tuple(rankings.get(query.query_id, ()))
        rows.append(
            {
                "id": query.query_id,
                "query": query.query,
                "category": query.category,
                "mode": query.mode.value,
                "exact": query.exact,
                "relevant_assets": dict(query.relevance),
                "top_10_asset_ids": list(ranked[:10]),
                "recall_at_5": recall_at_k(ranked, query.relevance, k=5),
                "recall_at_10": recall_at_k(ranked, query.relevance, k=10),
                "ndcg_at_10": ndcg_at_k(ranked, query.relevance, k=10),
            }
        )
    exact_rows = [item for item in rows if item["exact"]]
    return {
        "queries": rows,
        "exact_recall_at_5": (
            sum(float(item["recall_at_5"]) for item in exact_rows) / len(exact_rows)
            if exact_rows
            else 0.0
        ),
        "recall_at_10": (
            sum(float(item["recall_at_10"]) for item in rows) / len(rows)
            if rows
            else 0.0
        ),
        "ndcg_at_10": (
            sum(float(item["ndcg_at_10"]) for item in rows) / len(rows)
            if rows
            else 0.0
        ),
    }


def _timed_searches(
    runtime: SearchRuntime,
    queries: Sequence[SearchEvaluationQuery],
    *,
    mode: SearchMode,
    warmup_runs: int,
    measured_runs: int,
) -> list[float]:
    selected = [item for item in queries if item.exact] or list(queries)
    for _ in range(warmup_runs):
        for query in selected:
            runtime.service.search(
                query.query,
                mode=mode,
                limit=10,
            )
    latencies: list[float] = []
    for _ in range(measured_runs):
        for query in selected:
            started = perf_counter()
            runtime.service.search(
                query.query,
                mode=mode,
                limit=10,
            )
            latencies.append((perf_counter() - started) * 1_000)
    return latencies


def run_live_evaluation(
    settings: ApiSettings,
    queries: Sequence[SearchEvaluationQuery],
    *,
    warmup_runs: int,
    latency_runs: int,
    thresholds: SearchThresholds,
) -> dict[str, Any]:
    code_index_enabled = bool(getattr(settings, "search_code_index_enabled", False))
    evaluated_queries, skipped_queries = select_queries_for_index_scope(
        queries,
        code_index_enabled=code_index_enabled,
    )
    index_scope = build_index_scope(
        code_index_enabled=code_index_enabled,
        fixture_query_count=len(queries),
        evaluated_query_count=len(evaluated_queries),
        skipped_query_count=len(skipped_queries),
    )
    runtime = SearchRuntime.connect(settings)
    database = DatabaseManager(settings)
    try:
        expected_ids = {
            UUID(asset_id)
            for query in evaluated_queries
            for asset_id in query.relevance
        }
        with database.session() as session:
            present_ids = {
                str(item)
                for item in session.scalars(
                    select(Asset.id).where(
                        Asset.id.in_(expected_ids),
                        Asset.deleted_at.is_(None),
                    )
                ).all()
            }
        missing_truth_assets = sorted(
            str(item) for item in expected_ids - {UUID(v) for v in present_ids}
        )

        rankings: dict[str, tuple[str, ...]] = {}
        execution: list[dict[str, Any]] = []
        for query in evaluated_queries:
            result = runtime.service.search(
                query.query,
                mode=query.mode,
                limit=100,
            )
            rankings[query.query_id] = deduplicate_asset_ids(result.hits, limit=100)
            execution.append(
                {
                    "id": query.query_id,
                    "degraded": result.degraded,
                    "errors": list(result.errors),
                }
            )

        metrics = evaluate_rankings(evaluated_queries, rankings)
        structured_latencies = _timed_searches(
            runtime,
            evaluated_queries,
            mode=SearchMode.EXACT,
            warmup_runs=warmup_runs,
            measured_runs=latency_runs,
        )
        keyword_latencies = _timed_searches(
            runtime,
            evaluated_queries,
            mode=SearchMode.KEYWORD,
            warmup_runs=warmup_runs,
            measured_runs=latency_runs,
        )
        metrics["structured_p95_ms"] = percentile_95(structured_latencies)
        metrics["keyword_p95_ms"] = percentile_95(keyword_latencies)
        metrics["structured_sample_count"] = len(structured_latencies)
        metrics["keyword_sample_count"] = len(keyword_latencies)

        selected_index_label = ", ".join(
            str(name) for name in index_scope["selected_indexes"]
        )

        checks = {
            "truth_assets_present": not missing_truth_assets,
            "no_degraded_queries": not any(bool(item["degraded"]) for item in execution),
            "exact_recall_at_5": metrics["exact_recall_at_5"] >= thresholds.exact_recall_at_5,
            "recall_at_10": metrics["recall_at_10"] >= thresholds.recall_at_10,
            "ndcg_at_10": metrics["ndcg_at_10"] >= thresholds.ndcg_at_10,
            "structured_p95_ms": metrics["structured_p95_ms"] < thresholds.structured_p95_ms,
            "keyword_p95_ms": metrics["keyword_p95_ms"] < thresholds.keyword_p95_ms,
        }
        return {
            "status": "passed" if all(checks.values()) else "failed",
            "generated_at": datetime.now(UTC).isoformat(),
            "index_version": "meilisearch-keyword",
            "index_scope": index_scope,
            "skipped_queries": [dict(item) for item in skipped_queries],
            "missing_truth_assets": missing_truth_assets,
            "execution": execution,
            "metrics": metrics,
            "latency_methodology": {
                "structured": (
                    "Exact-mode identifier/title lookup; warm-cache nearest-rank P95."
                ),
                "keyword": (
                    "Keyword lookup over selected Meilisearch indexes "
                    f"({selected_index_label}); warm-cache nearest-rank P95."
                ),
            },
            "thresholds": asdict(thresholds),
            "checks": checks,
        }
    finally:
        database.dispose()
        runtime.close()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate fixed-query search recall, nDCG and warm-cache P95."
    )
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_TRUTH_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--latency-runs", type=int, default=20)
    return parser.parse_args()


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main() -> int:
    args = _arguments()
    if args.warmup_runs < 0 or args.latency_runs < 1:
        raise SystemExit("--warmup-runs must be non-negative and --latency-runs positive")
    fixture_path: Path = args.ground_truth
    try:
        queries = load_queries(fixture_path)
        report = run_live_evaluation(
            ApiSettings(),
            queries,
            warmup_runs=args.warmup_runs,
            latency_runs=args.latency_runs,
            thresholds=SearchThresholds(),
        )
        report["fixture"] = {
            "path": str(fixture_path.resolve()),
            "sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            "query_count": len(queries),
        }
    except Exception as error:
        report = {
            "status": "unavailable",
            "generated_at": datetime.now(UTC).isoformat(),
            "error": {
                "type": type(error).__name__,
                "message": (
                    "Search evaluation could not reach or validate a required "
                    "local component."
                ),
            },
        }
    _write_report(args.report, report)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
