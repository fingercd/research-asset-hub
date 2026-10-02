"""Evaluate frozen, incomplete keyword-search labels through the local read-only API."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "config" / "keyword-knowledge-evaluation-v2.json"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "work" / "validation" / "knowledge-goal-20261002"
SEARCH_URL = "http://127.0.0.1:8877/api/v1/knowledge/search"
REQUEST_TIMEOUT_SECONDS = 20
EXPECTED_SPLIT_COUNTS = {"dev": 24, "holdout": 16}
EXPECTED_CATEGORY_COUNTS = {
    "zh_mechanism": 8,
    "bilingual_term": 8,
    "exact_title": 8,
    "mixed_constraints": 8,
    "cross_domain": 4,
    "negative_probe": 4,
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Evaluation files must contain a JSON object.")
    return document


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_dataset(dataset: dict[str, Any], *, root: Path = PROJECT_ROOT) -> None:
    if dataset.get("schema") != "research-asset-hub.keyword-evaluation-dataset/v2":
        raise ValueError("Unsupported keyword evaluation dataset schema.")
    questions = dataset.get("questions")
    if not isinstance(questions, list) or len(questions) != 40:
        raise ValueError("The frozen evaluation requires 40 questions.")
    question_ids = [question.get("id") for question in questions]
    if len(set(question_ids)) != len(question_ids) or any(
        not question_id for question_id in question_ids
    ):
        raise ValueError("Question IDs must be present and unique.")

    split_counts = {
        split: sum(question.get("split") == split for question in questions)
        for split in EXPECTED_SPLIT_COUNTS
    }
    if split_counts != EXPECTED_SPLIT_COUNTS:
        raise ValueError(f"Unexpected split populations: {split_counts}.")
    category_counts = {
        category: sum(question.get("category") == category for question in questions)
        for category in EXPECTED_CATEGORY_COUNTS
    }
    if category_counts != EXPECTED_CATEGORY_COUNTS:
        raise ValueError(f"Unexpected category populations: {category_counts}.")

    corpus_ids = {
        asset_id for corpus in dataset["corpora"].values() for asset_id in corpus["asset_ids"]
    }
    for question in questions:
        if question.get("split") not in EXPECTED_SPLIT_COUNTS:
            raise ValueError(f"Invalid split on {question.get('id')}.")
        if not isinstance(question.get("query"), str) or not question["query"].strip():
            raise ValueError(f"Missing natural-language query on {question.get('id')}.")
        labels = question.get("known_relevant_asset_ids")
        if not isinstance(labels, list) or len(labels) != len(set(labels)):
            raise ValueError(f"Invalid labels on {question.get('id')}.")
        if question["category"] == "negative_probe":
            if labels or not question.get("negative_probe"):
                raise ValueError(
                    f"Negative probes need empty labels and an absence scope: {question['id']}."
                )
        elif not 1 <= len(labels) <= 3:
            raise ValueError(
                f"Expected one to three known relevant papers on {question.get('id')}."
            )
        if not set(labels) <= corpus_ids:
            raise ValueError(f"Unknown paper label on {question.get('id')}.")
        evidence_ids = {evidence.get("asset_id") for evidence in question.get("label_evidence", [])}
        if labels and not set(labels) <= evidence_ids:
            raise ValueError(f"A known relevant paper lacks evidence on {question.get('id')}.")

    if dataset.get("labels_complete") is not False:
        raise ValueError("Labels must remain explicitly incomplete.")
    if dataset.get("split_policy", {}).get("seed") != 20261002:
        raise ValueError("Unexpected frozen split seed.")
    for reference in dataset.get("input_files", []):
        path = root / reference["path"]
        if not path.is_file() or sha256_file(path) != reference["sha256"]:
            raise ValueError(f"Frozen input changed or missing: {reference['path']}.")


def questions_for_split(dataset: dict[str, Any], split: str) -> list[dict[str, Any]]:
    if split not in EXPECTED_SPLIT_COUNTS:
        raise ValueError(f"Unsupported split: {split}.")
    return [question for question in dataset["questions"] if question["split"] == split]


def summarize(rows: list[dict[str, Any]], *, planned_count: int | None = None) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if row["request_status"] == "ok"
        and row["known_relevant_asset_ids"]
        and row["known_relevant_recall_at_10"] is not None
    ]
    timings = [row["elapsed_ms"] for row in rows]
    negative_rows = [row for row in rows if row["category"] == "negative_probe"]
    returned_negative_candidates = [
        row for row in negative_rows if row["candidate_result_count"] > 0
    ]
    return {
        "planned_question_count": planned_count if planned_count is not None else len(rows),
        "attempted_question_count": len(rows),
        "successful_complete_response_count": sum(row["request_status"] == "ok" for row in rows),
        "partial_response_count": sum(row["request_status"] == "partial" for row in rows),
        "request_error_count": sum(row["request_status"] == "error" for row in rows),
        "recall_eligible_positive_question_count": len(eligible),
        "known_relevant_recall_at_10_macro": mean(
            row["known_relevant_recall_at_10"] for row in eligible
        )
        if eligible
        else None,
        "known_relevant_recall_at_10_micro": (
            sum(len(row["matched_known_relevant_asset_ids"]) for row in eligible)
            / sum(len(row["known_relevant_asset_ids"]) for row in eligible)
            if eligible
            else None
        ),
        "hit_at_10": mean(row["hit_at_10"] for row in eligible) if eligible else None,
        "negative_probe_count": len(negative_rows),
        "negative_probe_candidate_count": len(returned_negative_candidates),
        "negative_probe_manual_evidence_reviews_required": sum(
            row["substantive_evidence_status"] == "candidate_results_pending_manual_review"
            for row in negative_rows
        ),
        "latency_mean_ms": mean(timings) if timings else None,
        "latency_p95_ms": sorted(timings)[min(len(timings) - 1, int(len(timings) * 0.95))]
        if timings
        else None,
        "backends": sorted({str(row["backend"]) for row in rows if row["backend"] is not None}),
    }


def _make_row(
    question: dict[str, Any],
    *,
    status: str,
    http_status: int | None,
    elapsed_ms: float,
    payload: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = payload or {}
    items = response.get("items", [])[:10]
    returned_ids = [str(item.get("asset_id", item.get("id", ""))) for item in items]
    known = sorted(question["known_relevant_asset_ids"])
    matched = sorted(set(known).intersection(returned_ids))
    metric_eligible = status == "ok" and bool(known)
    negative_probe = question["category"] == "negative_probe"
    if negative_probe:
        evidence_status = (
            "no_candidates_returned"
            if status == "ok" and not items
            else "candidate_results_pending_manual_review"
            if items
            else "incomplete_response"
        )
        substantive_evidence_returned: bool | None = (
            False if evidence_status == "no_candidates_returned" else None
        )
    else:
        evidence_status = None
        substantive_evidence_returned = None
    return {
        "question_id": question["id"],
        "category": question["category"],
        "query": question["query"],
        "known_relevant_asset_ids": known,
        "request_status": status,
        "evaluation_eligible": metric_eligible,
        "http_status": http_status,
        "error": error,
        "elapsed_ms": round(elapsed_ms, 3),
        "backend": response.get("backend"),
        "degraded": response.get("degraded"),
        "backend_error_count": len(response.get("errors", [])),
        "searched_queries": response.get("searched_queries", []),
        "returned_asset_ids": returned_ids,
        "returned_titles": [item.get("title") for item in items],
        "candidate_result_count": len(items),
        "matched_known_relevant_asset_ids": matched,
        "known_relevant_recall_at_10": len(matched) / len(known) if metric_eligible else None,
        "hit_at_10": int(bool(matched)) if metric_eligible else None,
        "substantive_evidence_returned": substantive_evidence_returned,
        "substantive_evidence_status": evidence_status,
    }


def evaluate(dataset: dict[str, Any], *, split: str) -> list[dict[str, Any]]:
    questions = questions_for_split(dataset, split)
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False) as client:
        for question in questions:
            started = perf_counter()
            payload: dict[str, Any] = {}
            http_status: int | None = None
            status = "ok"
            error: dict[str, Any] | None = None
            try:
                response = client.get(
                    SEARCH_URL,
                    params={"query": question["query"], "asset_type": "paper", "limit": 10},
                )
                http_status = response.status_code
                response.raise_for_status()
                decoded = response.json()
                if not isinstance(decoded, dict) or not isinstance(decoded.get("items", []), list):
                    raise ValueError("Search response must be an object with an items list.")
                payload = decoded
                if payload.get("degraded") is True or payload.get("errors"):
                    status = "partial"
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exception:
                status = "error"
                error = {"type": type(exception).__name__, "http_status": http_status}

            elapsed_ms = (perf_counter() - started) * 1000
            row = _make_row(
                question,
                status=status,
                http_status=http_status,
                elapsed_ms=elapsed_ms,
                payload=payload,
                error=error,
            )
            rows.append(row)
            print(
                json.dumps(
                    {
                        "question_id": row["question_id"],
                        "request_status": status,
                        "evaluation_eligible": row["evaluation_eligible"],
                        "elapsed_ms": row["elapsed_ms"],
                        "error": error,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if status != "ok":
                break
    return rows


def build_report(
    dataset: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    split: str,
    dataset_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any]:
    planned = questions_for_split(dataset, split)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)
    return {
        "schema": "research-asset-hub.keyword-evaluation-report/v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "split": split,
        "endpoint": SEARCH_URL,
        "http_method": "GET",
        "asset_type": "paper",
        "limit": 10,
        "read_only": True,
        "trust_env": False,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "stop_after_first_partial_or_error": True,
        "planned_question_count": len(planned),
        "attempted_question_count": len(rows),
        "stopped_early": len(rows) < len(planned),
        "remaining_question_count": len(planned) - len(rows),
        "dataset_sha256": dataset_sha256,
        "evaluator_sha256": evaluator_sha256,
        "labels_complete": False,
        "label_caveat": (
            "每题 1–3 篇由独立 Agent 依据来源预标，未经过人工金标准审核；不代表全库穷尽；"
            "未標論文不視為不相關。部分或錯誤響應不計入召回率。"
        ),
        "negative_probe_caveat": (
            "负例仅探测本次候选结果；有结果时须结合原文审核是否含实质证据，"
            "零结果只表示本次未返回候选，不证明库外不存在。"
        ),
        "summary": summarize(rows, planned_count=len(planned)),
        "by_category": {
            category: summarize(category_rows)
            for category, category_rows in sorted(by_category.items())
        },
        "questions": rows,
    }


def run_and_save_report(
    dataset: dict[str, Any],
    *,
    split: str,
    output_path: Path,
    dataset_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any]:
    if output_path.exists():
        raise ValueError("Report exists; choose a new output path to preserve prior results.")
    rows = evaluate(dataset, split=split)
    report = build_report(
        dataset,
        rows,
        split=split,
        dataset_sha256=dataset_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    write_json(output_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument(
        "--freeze-file", type=Path, default=DEFAULT_REPORT_ROOT / "eval-freeze.json"
    )
    parser.add_argument("--authorize-holdout", action="store_true")
    args = parser.parse_args()

    dataset = read_json(args.dataset)
    validate_dataset(dataset)
    dataset_sha256 = sha256_file(args.dataset)
    evaluator_sha256 = sha256_file(Path(__file__))
    if args.freeze_only:
        if args.freeze_file.exists():
            raise ValueError(
                "Freeze manifest already exists; refuse to replace the frozen evaluation."
            )
        write_json(
            args.freeze_file,
            {
                "schema": "research-asset-hub.keyword-evaluation-freeze/v2",
                "frozen_at": datetime.now(UTC).isoformat(),
                "dataset_sha256": dataset_sha256,
                "evaluator_sha256": evaluator_sha256,
                "split_counts": EXPECTED_SPLIT_COUNTS,
                "category_counts": EXPECTED_CATEGORY_COUNTS,
                "split_seed": 20261002,
                "labels_complete": False,
                "input_files": dataset["input_files"],
            },
        )
        print(json.dumps({"freeze_file": str(args.freeze_file), "dataset_sha256": dataset_sha256}))
        return 0

    frozen = read_json(args.freeze_file)
    if frozen.get("dataset_sha256") != dataset_sha256:
        raise ValueError("Dataset differs from its frozen SHA-256 fingerprint.")
    if frozen.get("evaluator_sha256") != evaluator_sha256:
        raise ValueError("Evaluator differs from its frozen SHA-256 fingerprint.")
    if args.split == "holdout" and not args.authorize_holdout:
        parser.error("Holdout execution requires explicit --authorize-holdout.")

    output_path = args.output or DEFAULT_REPORT_ROOT / f"eval-{args.split}.json"
    report = run_and_save_report(
        dataset,
        split=args.split,
        output_path=output_path,
        dataset_sha256=dataset_sha256,
        evaluator_sha256=evaluator_sha256,
    )
    print(
        json.dumps(
            {"report": str(output_path), "summary": report["summary"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
