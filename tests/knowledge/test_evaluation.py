from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V3_DATASET = PROJECT_ROOT / "config" / "keyword-knowledge-evaluation-v3.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import evaluate_keyword_knowledge as evaluation  # noqa: E402


def _question(
    question_id: str,
    *,
    split: str,
    category: str = "zh_mechanism",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": question_id,
        "split": split,
        "category": category,
        "query": f"natural query {question_id}",
        "known_relevant_asset_ids": labels or [],
    }


class _Response:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", evaluation.SEARCH_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("mock status", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return self.payload


class _Client:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, *, params: dict[str, Any]) -> _Response:
        self.calls.append((url, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_dataset_hash_and_frozen_inputs_are_valid(local_research_assets: None) -> None:
    dataset = evaluation.read_json(V3_DATASET)
    evaluation.validate_dataset(dataset)
    digest = evaluation.sha256_file(V3_DATASET)
    expected = hashlib.sha256(V3_DATASET.read_bytes()).hexdigest()
    assert digest == expected
    assert len(digest) == 64


def test_dev_selection_does_not_include_holdout() -> None:
    dataset = evaluation.read_json(V3_DATASET)
    dev = evaluation.questions_for_split(dataset, "dev")
    holdout = evaluation.questions_for_split(dataset, "holdout")
    dev_ids = {question["id"] for question in dev}
    holdout_ids = {question["id"] for question in holdout}
    assert len(dev) == 24
    assert len(holdout) == 16
    assert dev_ids.isdisjoint(holdout_ids)


def test_v3_revises_only_mechanism_queries_and_preserves_v2_labels() -> None:
    v2 = evaluation.read_json(PROJECT_ROOT / "config" / "keyword-knowledge-evaluation-v2.json")
    v3 = evaluation.read_json(V3_DATASET)
    old_questions = {question["id"]: question for question in v2["questions"]}
    new_questions = {question["id"]: question for question in v3["questions"]}
    revised_ids = {
        question["id"] for question in v3["questions"] if question["category"] == "zh_mechanism"
    }
    if len(revised_ids) != 8 or set(old_questions) != set(new_questions):
        raise AssertionError("The revision must keep 40 question IDs and eight mechanism items.")
    for question_id in set(old_questions) - revised_ids:
        if old_questions[question_id] != new_questions[question_id]:
            raise AssertionError(f"A non-mechanism item changed: {question_id}")
    for question_id in revised_ids:
        old_question = {
            key: value for key, value in old_questions[question_id].items() if key != "query"
        }
        new_question = {
            key: value for key, value in new_questions[question_id].items() if key != "query"
        }
        if old_question != new_question:
            raise AssertionError(f"Labels, split, or evidence changed: {question_id}")
        if not new_questions[question_id]["query"]:
            raise AssertionError(f"Mechanism query is empty: {question_id}")
        if any(char.isascii() and char.isalnum() for char in new_questions[question_id]["query"]):
            raise AssertionError(
                f"Mechanism query contains an ASCII name, acronym, or number: {question_id}"
            )


def test_evaluate_sends_only_dev_queries_with_fixed_read_only_contract(monkeypatch: Any) -> None:
    dataset = {
        "questions": [
            _question("D1", split="dev", labels=["paper-1"]),
            _question("H1", split="holdout", labels=["paper-2"]),
        ]
    }
    client = _Client([_Response({"items": [], "degraded": False})])
    captured: dict[str, Any] = {}

    def make_client(*, timeout: float, trust_env: bool) -> _Client:
        captured.update(timeout=timeout, trust_env=trust_env)
        return client

    monkeypatch.setattr(evaluation.httpx, "Client", make_client)
    rows = evaluation.evaluate(dataset, split="dev")

    assert [row["question_id"] for row in rows] == ["D1"]
    assert client.calls == [
        (
            "http://127.0.0.1:8877/api/v1/knowledge/search",
            {"query": "natural query D1", "asset_type": "paper", "limit": 10},
        )
    ]
    assert captured == {"timeout": 20, "trust_env": False}


def test_partial_and_error_responses_are_excluded_from_recall_denominator() -> None:
    rows = [
        {
            "category": "zh_mechanism",
            "request_status": "ok",
            "known_relevant_asset_ids": ["A", "B"],
            "matched_known_relevant_asset_ids": ["A"],
            "known_relevant_recall_at_10": 0.5,
            "hit_at_10": 1,
            "elapsed_ms": 5.0,
            "backend": "primary",
            "candidate_result_count": 1,
            "substantive_evidence_status": None,
        },
        {
            "category": "bilingual_term",
            "request_status": "partial",
            "known_relevant_asset_ids": ["C"],
            "matched_known_relevant_asset_ids": ["C"],
            "known_relevant_recall_at_10": None,
            "hit_at_10": None,
            "elapsed_ms": 7.0,
            "backend": "fallback",
            "candidate_result_count": 1,
            "substantive_evidence_status": None,
        },
        {
            "category": "exact_title",
            "request_status": "error",
            "known_relevant_asset_ids": ["D"],
            "matched_known_relevant_asset_ids": [],
            "known_relevant_recall_at_10": None,
            "hit_at_10": None,
            "elapsed_ms": 2.0,
            "backend": None,
            "candidate_result_count": 0,
            "substantive_evidence_status": None,
        },
        {
            "category": "negative_probe",
            "request_status": "ok",
            "known_relevant_asset_ids": [],
            "matched_known_relevant_asset_ids": [],
            "known_relevant_recall_at_10": None,
            "hit_at_10": None,
            "elapsed_ms": 6.0,
            "backend": "primary",
            "candidate_result_count": 2,
            "substantive_evidence_status": "candidate_results_pending_manual_review",
        },
    ]

    summary = evaluation.summarize(rows, planned_count=8)

    assert summary["planned_question_count"] == 8
    assert summary["attempted_question_count"] == 4
    assert summary["partial_response_count"] == 1
    assert summary["request_error_count"] == 1
    assert summary["recall_eligible_positive_question_count"] == 1
    assert summary["known_relevant_recall_at_10_macro"] == 0.5
    assert summary["known_relevant_recall_at_10_micro"] == 0.5
    assert summary["hit_at_10"] == 1
    assert summary["negative_probe_candidate_count"] == 1
    assert summary["negative_probe_manual_evidence_reviews_required"] == 1


def test_first_partial_response_stops_and_saves_partial_report(
    monkeypatch: Any, tmp_path: Path
) -> None:
    dataset = {
        "questions": [
            _question("D1", split="dev", labels=["paper-1"]),
            _question("D2", split="dev", labels=["paper-2"]),
        ]
    }
    client = _Client(
        [
            _Response(
                {
                    "items": [{"asset_id": "paper-1", "title": "Paper 1"}],
                    "degraded": True,
                    "errors": [{"backend": "primary"}],
                }
            )
        ]
    )
    monkeypatch.setattr(
        evaluation.httpx,
        "Client",
        lambda **_kwargs: client,
    )
    output_path = tmp_path / "eval-dev.json"

    report = evaluation.run_and_save_report(
        dataset,
        split="dev",
        output_path=output_path,
        dataset_sha256="dataset-hash",
        evaluator_sha256="evaluator-hash",
    )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(client.calls) == 1
    assert report["stopped_early"] is True
    assert report["remaining_question_count"] == 1
    assert saved["questions"][0]["request_status"] == "partial"
    assert saved["questions"][0]["known_relevant_recall_at_10"] is None
    assert saved["summary"]["recall_eligible_positive_question_count"] == 0


def test_first_request_error_is_saved_without_treating_it_as_a_miss(
    monkeypatch: Any, tmp_path: Path
) -> None:
    dataset = {
        "questions": [
            _question("D1", split="dev", labels=["paper-1"]),
            _question("D2", split="dev", labels=["paper-2"]),
        ]
    }
    client = _Client([httpx.ConnectError("offline")])
    monkeypatch.setattr(evaluation.httpx, "Client", lambda **_kwargs: client)
    output_path = tmp_path / "eval-dev-error.json"

    report = evaluation.run_and_save_report(
        dataset,
        split="dev",
        output_path=output_path,
        dataset_sha256="dataset-hash",
        evaluator_sha256="evaluator-hash",
    )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(client.calls) == 1
    assert saved["questions"][0]["request_status"] == "error"
    assert saved["questions"][0]["known_relevant_recall_at_10"] is None
    assert report["summary"]["known_relevant_recall_at_10_macro"] is None
    assert report["stopped_early"] is True
