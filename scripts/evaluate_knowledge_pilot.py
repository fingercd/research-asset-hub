"""Evaluate frozen, incomplete relevance labels through the read-only knowledge API."""

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
DEFAULT_DATASET = PROJECT_ROOT / "config" / "knowledge-pilot-evaluation.json"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "work" / "validation" / "knowledge-pilot-20261002"


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


def validate_dataset(dataset: dict[str, Any]) -> None:
    questions = dataset["questions"]
    if len(questions) != 30 or len({q["id"] for q in questions}) != 30:
        raise ValueError("Evaluation requires exactly 30 uniquely identified questions.")
    for split, expected in (("dev", 18), ("holdout", 12)):
        if sum(q["split"] == split for q in questions) != expected:
            raise ValueError(f"Unexpected {split} population.")
    for category in ("zh_mechanism", "exact_method", "mixed_constraints"):
        if sum(q["category"] == category for q in questions) != 10:
            raise ValueError(f"Unexpected category population: {category}.")
    paper_ids = set(dataset["pilot_asset_ids"])
    for question in questions:
        labels = question["known_relevant_asset_ids"]
        if not 1 <= len(labels) <= 3 or not set(labels) <= paper_ids:
            raise ValueError(f"Invalid labels for {question['id']}.")
    for reference in dataset["input_files"]:
        path = PROJECT_ROOT / reference["path"]
        if sha256_file(path) != reference["sha256"]:
            raise ValueError(f"Frozen input changed: {reference['path']}.")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row["request_status"] == "ok"]
    timings = sorted(row["elapsed_ms"] for row in rows)
    return {
        "question_count": len(rows),
        "successful_request_count": len(successful),
        "request_error_count": len(rows) - len(successful),
        "degraded_count": sum(row["degraded"] is True for row in rows),
        "known_relevant_recall_at_10_macro": mean(
            row["known_relevant_recall_at_10"] for row in rows
        )
        if rows
        else None,
        "hit_at_10": mean(row["hit_at_10"] for row in rows) if rows else None,
        "latency_mean_ms": mean(timings) if timings else None,
        "latency_p95_ms": timings[min(len(timings) - 1, int(len(timings) * 0.95))]
        if timings
        else None,
        "backends": sorted({str(row["backend"]) for row in rows}),
    }


def evaluate(
    dataset: dict[str, Any],
    *,
    split: str,
    base_url: str,
    stop_on_request_error: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client:
        for question in dataset["questions"]:
            if question["split"] != split:
                continue
            started = perf_counter()
            status = "ok"
            payload: dict[str, Any] = {}
            error: dict[str, Any] | None = None
            http_status: int | None = None
            try:
                response = client.get(
                    "/api/v1/knowledge/search",
                    params={"query": question["query"], "asset_type": "paper", "limit": 10},
                )
                http_status = response.status_code
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exception:
                status = "error"
                error = {"type": type(exception).__name__, "http_status": http_status}
            elapsed_ms = (perf_counter() - started) * 1000
            items = payload.get("items", [])[:10]
            returned_ids = [str(item.get("asset_id", item.get("id", ""))) for item in items]
            relevant = set(question["known_relevant_asset_ids"])
            matched = sorted(relevant.intersection(returned_ids))
            row = {
                "question_id": question["id"],
                "category": question["category"],
                "query": question["query"],
                "known_relevant_asset_ids": sorted(relevant),
                "request_status": status,
                "http_status": http_status,
                "error": error,
                "elapsed_ms": round(elapsed_ms, 3),
                "backend": payload.get("backend"),
                "degraded": payload.get("degraded"),
                "backend_errors": payload.get("errors", []),
                "searched_queries": payload.get("searched_queries", []),
                "returned_asset_ids": returned_ids,
                "returned_titles": [item.get("title") for item in items],
                "matched_known_relevant_asset_ids": matched,
                "known_relevant_recall_at_10": len(matched) / len(relevant),
                "hit_at_10": int(bool(matched)),
            }
            rows.append(row)
            print(
                json.dumps(
                    {
                        "question_id": question["id"],
                        "backend": row["backend"],
                        "degraded": row["degraded"],
                        "hit_at_10": row["hit_at_10"],
                        "elapsed_ms": row["elapsed_ms"],
                        "error": error,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if status == "error" and stop_on_request_error:
                break
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--base-url", default="http://127.0.0.1:8877")
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--authorize-holdout", action="store_true")
    parser.add_argument("--stop-on-request-error", action="store_true")
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument(
        "--freeze-file", type=Path, default=DEFAULT_REPORT_ROOT / "retrieval-dataset-freeze.json"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    dataset = read_json(args.dataset)
    validate_dataset(dataset)
    dataset_digest = sha256_file(args.dataset)
    script_digest = sha256_file(Path(__file__))
    if args.freeze_only:
        if args.freeze_file.exists():
            raise ValueError("Freeze manifest already exists; refuse to replace frozen evaluation.")
        write_json(
            args.freeze_file,
            {
                "schema": "research-asset-hub.knowledge-retrieval-freeze/v1",
                "frozen_at": datetime.now(UTC).isoformat(),
                "dataset_sha256": dataset_digest,
                "evaluator_sha256": script_digest,
                "split_counts": {"dev": 18, "holdout": 12},
                "labels_complete": False,
                "input_files": dataset["input_files"],
            },
        )
        print(json.dumps({"frozen": str(args.freeze_file), "dataset_sha256": dataset_digest}))
        return 0
    frozen = read_json(args.freeze_file)
    if frozen["dataset_sha256"] != dataset_digest:
        raise ValueError("Dataset differs from the pre-run frozen hash.")
    if frozen["evaluator_sha256"] != script_digest:
        raise ValueError("Evaluator differs from the pre-run frozen hash.")
    if args.split == "holdout" and not args.authorize_holdout:
        parser.error("Holdout execution needs explicit --authorize-holdout.")
    report = args.report or DEFAULT_REPORT_ROOT / f"retrieval-{args.split}.json"
    if report.exists():
        raise ValueError("Report exists; select a new report filename to preserve prior results.")
    rows = evaluate(
        dataset,
        split=args.split,
        base_url=args.base_url,
        stop_on_request_error=args.stop_on_request_error,
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)
    write_json(
        report,
        {
            "schema": "research-asset-hub.knowledge-retrieval-evaluation/v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "split": args.split,
            "base_url": args.base_url,
            "read_only": True,
            "trust_env": False,
            "request_timeout_seconds": 30,
            "stop_on_request_error": args.stop_on_request_error,
            "planned_question_count": sum(q["split"] == args.split for q in dataset["questions"]),
            "stopped_early": len(rows)
            < sum(q["split"] == args.split for q in dataset["questions"]),
            "dataset_sha256": dataset_digest,
            "evaluator_sha256": script_digest,
            "labels_complete": False,
            "label_caveat": (
                "预标标签仅是不完备的已知相关论文集合，非全库穷尽相关性判断；"
                "未标结果不计为不相关。"
            ),
            "summary": summarize(rows),
            "by_category": {key: summarize(value) for key, value in grouped.items()},
            "questions": rows,
        },
    )
    print(json.dumps({"report": str(report), "summary": summarize(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
