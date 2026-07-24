from __future__ import annotations

from packages.rag import (
    DualProviderEvaluator,
    EvaluationObservation,
    EvaluationQuestion,
)


def test_dual_provider_evaluation_runs_30_questions_and_prefers_cheaper_close_score() -> None:
    questions = [EvaluationQuestion(f"q-{index}", "question") for index in range(30)]

    def runner(provider: str, question: EvaluationQuestion) -> EvaluationObservation:
        del question
        if provider == "primary":
            return EvaluationObservation(
                faithfulness=0.99,
                completeness=0.98,
                factual_claims=2,
                cited_claims=2,
                invalid_citations=0,
                cost=0.10,
                latency_ms=100,
            )
        return EvaluationObservation(
            faithfulness=0.98,
            completeness=0.98,
            factual_claims=2,
            cited_claims=2,
            invalid_citations=0,
            cost=0.05,
            latency_ms=100,
        )

    report = DualProviderEvaluator().evaluate(
        ("primary", "fallback"),
        questions,
        runner,
    )
    assert report.question_count == 30
    assert report.winner == "fallback"
    assert all(not item.disqualified for item in report.results)


def test_invalid_or_low_coverage_citations_disqualify_provider() -> None:
    questions = [EvaluationQuestion(f"q-{index}", "question") for index in range(30)]

    def runner(provider: str, question: EvaluationQuestion) -> EvaluationObservation:
        index = int(question.question_id.split("-")[1])
        if provider == "unsafe":
            return EvaluationObservation(
                faithfulness=1,
                completeness=1,
                factual_claims=1,
                cited_claims=0 if index < 2 else 1,
                invalid_citations=1 if index == 0 else 0,
                cost=0,
                latency_ms=1,
            )
        return EvaluationObservation(
            faithfulness=0.9,
            completeness=0.9,
            factual_claims=1,
            cited_claims=1,
            invalid_citations=0,
            cost=1,
            latency_ms=10,
        )

    report = DualProviderEvaluator().evaluate(
        ("unsafe", "safe"),
        questions,
        runner,
    )
    assert report.results[0].disqualified is True
    assert report.winner == "safe"

