from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.rag import (
    Citation,
    RAGAnswer,
    RagServiceResult,
)
from scripts.evaluate_rag import (
    RagEvaluationQuestion,
    aggregate_records,
    inspect_result,
    load_questions,
)


def _question() -> RagEvaluationQuestion:
    return RagEvaluationQuestion(
        question_id="q-1",
        question="方法有什么贡献？",
        asset_ids=(UUID("019f93e5-206a-798c-a924-389005f1d674"),),
        category="contribution",
    )


def _result(
    *,
    text: str = "方法改进了控制性能。[citation:chunk-1]",
    stop_reason: str = "completed",
) -> RagServiceResult:
    citation = Citation(
        citation_id="chunk-1",
        asset_id="019f93e5-206a-798c-a924-389005f1d674",
        document_id="chunk:chunk-1",
        representation_id="019f93e5-206a-798c-a924-389005f1d675",
        anchor={"kind": "pdf_page", "page": 2},
        title="Paper",
        url="/assets/019f93e5-206a-798c-a924-389005f1d674/documents/chunk",
        chunk_id="019f93e5-206a-798c-a924-389005f1d676",
        chunk_hash="a" * 64,
        representation_digest="b" * 64,
    )
    citations = (citation,) if stop_reason == "completed" else ()
    return RagServiceResult(
        answer=RAGAnswer(
            text=text,
            citations=citations,
            provider="deepseek" if stop_reason == "completed" else "none",
            model="deepseek-v4-pro" if stop_reason == "completed" else "none",
            evidence_insufficient=stop_reason != "completed",
            input_tokens=100 if stop_reason == "completed" else 0,
            output_tokens=20 if stop_reason == "completed" else 0,
        ),
        retrieved=(),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        audit={
            "provider_attempts": [
                {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "outcome": stop_reason,
                    "actual_cost": 0.02,
                }
            ]
        },
    )


def test_fixed_rag_fixture_has_exactly_30_unique_scoped_questions() -> None:
    fixture = Path(__file__).parents[1] / "search-evaluation" / "rag-questions.yaml"
    questions = load_questions(fixture)
    assert len(questions) == 30
    assert len({item.question_id for item in questions}) == 30
    assert all(item.asset_ids for item in questions)


def test_result_inspection_scores_citations_without_retaining_answer_text() -> None:
    private_answer = "方法改进了控制性能。[citation:chunk-1]"
    record = inspect_result(
        _question(),
        _result(text=private_answer),
        latency_ms=123.0,
        citation_validator=lambda citation: (),
    )
    assert record.citation_coverage == 1.0
    assert record.invalid_citations == 0
    assert record.cost_cny == 0.02
    assert private_answer not in str(record)
    report = aggregate_records(
        [record] * 30,
        expected_question_count=30,
        backend_degraded=False,
    )
    assert report["status"] == "passed"
    assert report["metrics"]["citation_coverage"] == 1.0


def test_model_or_budget_block_is_reported_explicitly() -> None:
    blocked = inspect_result(
        _question(),
        _result(text="资料库证据不足", stop_reason="no_model"),
        latency_ms=1.0,
        citation_validator=lambda citation: (),
    )
    report = aggregate_records(
        [blocked],
        expected_question_count=30,
        backend_degraded=False,
        blocked_reason="no_model",
    )
    assert report["status"] == "blocked"
    assert report["blocked"]["reason"] == "no_model"
    assert report["checks"]["all_30_questions_attempted"] is False
    assert report["metrics"]["invalid_citations"] == 0
