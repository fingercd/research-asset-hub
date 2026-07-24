"""Deterministic 30-question dual-provider evaluation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvaluationQuestion:
    question_id: str
    question: str


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """One provider's externally scored result for one question."""

    faithfulness: float
    completeness: float
    factual_claims: int
    cited_claims: int
    invalid_citations: int
    cost: float
    latency_ms: float

    def __post_init__(self) -> None:
        if not 0 <= self.faithfulness <= 1:
            raise ValueError("faithfulness must be in [0, 1]")
        if not 0 <= self.completeness <= 1:
            raise ValueError("completeness must be in [0, 1]")
        if self.factual_claims < 0 or self.cited_claims < 0:
            raise ValueError("claim counts cannot be negative")
        if self.cited_claims > self.factual_claims:
            raise ValueError("cited_claims cannot exceed factual_claims")
        if self.invalid_citations < 0 or self.cost < 0 or self.latency_ms < 0:
            raise ValueError("evaluation measurements cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderEvaluation:
    provider: str
    score: float
    faithfulness: float
    completeness: float
    citation_coverage: float
    invalid_citations: int
    total_cost: float
    mean_latency_ms: float
    disqualified: bool


@dataclass(frozen=True, slots=True)
class DualProviderEvaluation:
    results: tuple[ProviderEvaluation, ProviderEvaluation]
    winner: str | None
    question_count: int = 30


EvaluationRunner = Callable[[str, EvaluationQuestion], EvaluationObservation]


class DualProviderEvaluator:
    """Apply the locked v1 weighting and eligibility gates.

    Grounding (faithfulness plus citation coverage) is 50%, completeness 25%,
    cost 15% and latency 10%.  A provider is disqualified for citation coverage
    below 95% or any invalid citation.  Scores within three percentage points
    choose the cheaper eligible provider.
    """

    def evaluate(
        self,
        providers: Sequence[str],
        questions: Sequence[EvaluationQuestion],
        runner: EvaluationRunner,
    ) -> DualProviderEvaluation:
        if len(providers) != 2 or len(set(providers)) != 2:
            raise ValueError("exactly two distinct providers are required")
        if len(questions) != 30 or len({item.question_id for item in questions}) != 30:
            raise ValueError("exactly 30 uniquely identified questions are required")

        observations = {
            provider: [runner(provider, question) for question in questions]
            for provider in providers
        }
        costs = {
            provider: sum(item.cost for item in values)
            for provider, values in observations.items()
        }
        latencies = {
            provider: sum(item.latency_ms for item in values) / len(values)
            for provider, values in observations.items()
        }
        min_cost = min(costs.values())
        min_latency = min(latencies.values())

        results: list[ProviderEvaluation] = []
        for provider in providers:
            values = observations[provider]
            faithfulness = sum(item.faithfulness for item in values) / len(values)
            completeness = sum(item.completeness for item in values) / len(values)
            total_claims = sum(item.factual_claims for item in values)
            cited_claims = sum(item.cited_claims for item in values)
            citation_coverage = (
                cited_claims / total_claims if total_claims else 1.0
            )
            invalid_citations = sum(item.invalid_citations for item in values)
            cost_score = 1.0 if costs[provider] == 0 else min_cost / costs[provider]
            latency_score = (
                1.0 if latencies[provider] == 0 else min_latency / latencies[provider]
            )
            grounding = (faithfulness + citation_coverage) / 2
            score = (
                grounding * 0.50
                + completeness * 0.25
                + cost_score * 0.15
                + latency_score * 0.10
            )
            results.append(
                ProviderEvaluation(
                    provider=provider,
                    score=score,
                    faithfulness=faithfulness,
                    completeness=completeness,
                    citation_coverage=citation_coverage,
                    invalid_citations=invalid_citations,
                    total_cost=costs[provider],
                    mean_latency_ms=latencies[provider],
                    disqualified=(
                        citation_coverage < 0.95 or invalid_citations > 0
                    ),
                )
            )

        eligible = [result for result in results if not result.disqualified]
        winner: str | None = None
        if eligible:
            eligible.sort(key=lambda item: (-item.score, item.total_cost, item.provider))
            winner = eligible[0].provider
            if len(eligible) == 2 and abs(eligible[0].score - eligible[1].score) <= 0.03:
                winner = min(
                    eligible,
                    key=lambda item: (item.total_cost, -item.score, item.provider),
                ).provider
        return DualProviderEvaluation(
            results=(results[0], results[1]),
            winner=winner,
        )

