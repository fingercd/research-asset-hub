"""Production RAG orchestration over caller-supplied retrieval candidates."""

from __future__ import annotations

from collections.abc import Callable

from packages.indexing.fusion import reciprocal_rank_fusion

from .budget import (
    BudgetEstimate,
    BudgetExceeded,
    BudgetLedger,
    PricingCatalog,
    estimate_budget,
)
from .models import (
    Citation,
    RAGAnswer,
    RagRequest,
    RagServiceResult,
    RetrievedEvidence,
)
from .providers import (
    ProviderCallError,
    ProviderChain,
    ProviderUnavailable,
    RetryableProviderError,
)
from .rerank import lexical_rerank
from .security import (
    PROMPT_VERSION,
    ValidatedEvidence,
    build_grounded_messages,
    sha256_json,
    validate_provider_citations,
    validate_search_hit,
)

_INSUFFICIENT = "资料库证据不足"

class RagService:
    """Fuse, validate, isolate, generate and validate again.

    Search and graph clients remain outside this class.  They pass their
    independently ranked results via :class:`UnifiedCandidates`, which also
    makes deterministic offline evaluation straightforward.
    """

    def __init__(
        self,
        providers: ProviderChain,
        pricing: PricingCatalog,
        *,
        budget: BudgetLedger | None = None,
        current_digest_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.providers = providers
        self.pricing = pricing
        self.budget = budget or BudgetLedger()
        self.current_digest_resolver = current_digest_resolver

    @staticmethod
    def _answer(
        *,
        text: str = _INSUFFICIENT,
        provider: str = "none",
        model: str = "none",
        citations: tuple[Citation, ...] = (),
        insufficient: bool = True,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> RAGAnswer:
        return RAGAnswer(
            text=text,
            citations=citations,
            provider=provider,
            model=model,
            evidence_insufficient=insufficient,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    @staticmethod
    def _actual_estimate(
        *,
        input_tokens: int,
        output_tokens: int,
        pricing_input: float,
        pricing_output: float,
    ) -> BudgetEstimate:
        cost = (
            input_tokens * pricing_input + output_tokens * pricing_output
        ) / 1_000_000
        return BudgetEstimate(input_tokens, output_tokens, cost)

    @staticmethod
    def _audit_base(
        request: RagRequest,
        evidence: tuple[ValidatedEvidence, ...],
        invalid_reasons: dict[str, int],
    ) -> dict[str, object]:
        return {
            "prompt_version": PROMPT_VERSION,
            "question_hash": sha256_json(request.question),
            "filters_hash": sha256_json(request.filters),
            "index_versions": dict(request.candidates.index_versions),
            "retrieval": [
                {
                    "chunk_id": item.reference.chunk_id,
                    "chunk_hash": item.reference.chunk_hash,
                    "representation_digest": item.reference.representation_digest,
                    "score": item.reference.score,
                    "sources": list(item.reference.sources),
                }
                for item in evidence
            ],
            "invalid_evidence": dict(sorted(invalid_reasons.items())),
            "provider_attempts": [],
        }

    def _validated_evidence(
        self,
        request: RagRequest,
    ) -> tuple[tuple[ValidatedEvidence, ...], dict[str, int]]:
        if request.max_evidence < 1:
            raise ValueError("max_evidence must be positive")
        result_sets = request.candidates.result_sets()
        if not result_sets:
            return (), {}
        fused = reciprocal_rank_fusion(
            result_sets,
            k=60,
            limit=max(30, request.max_evidence * 4),
        )
        reranked = lexical_rerank(
            request.question,
            [
                RetrievedEvidence(hit=hit, citation_id=hit.document.document_id)
                for hit in fused
            ],
        )
        valid: list[ValidatedEvidence] = []
        seen_chunks: set[str] = set()
        invalid: dict[str, int] = {}
        for candidate in reranked:
            validation = validate_search_hit(candidate.hit)
            if validation.value is None:
                reason = validation.reason or "unknown"
                invalid[reason] = invalid.get(reason, 0) + 1
                continue
            if self.current_digest_resolver is not None:
                try:
                    current = self.current_digest_resolver(
                        validation.value.reference.representation_id
                    )
                except Exception:
                    invalid["freshness_check_failed"] = (
                        invalid.get("freshness_check_failed", 0) + 1
                    )
                    continue
                if (
                    current is None
                    or current.lower()
                    != validation.value.reference.representation_digest
                ):
                    invalid["stale_representation"] = (
                        invalid.get("stale_representation", 0) + 1
                    )
                    continue
            chunk_id = validation.value.reference.chunk_id
            if chunk_id in seen_chunks:
                continue
            seen_chunks.add(chunk_id)
            valid.append(validation.value)
            if len(valid) >= request.max_evidence:
                break
        return tuple(valid), invalid

    def _evidence_is_current(self, evidence: tuple[ValidatedEvidence, ...]) -> bool:
        if self.current_digest_resolver is None:
            return True
        try:
            for item in evidence:
                current = self.current_digest_resolver(
                    item.reference.representation_id
                )
                if (
                    current is None
                    or current.lower() != item.reference.representation_digest
                ):
                    return False
        except Exception:
            return False
        return True

    def answer(self, request: RagRequest) -> RagServiceResult:
        evidence, invalid_reasons = self._validated_evidence(request)
        audit = self._audit_base(request, evidence, invalid_reasons)
        retrieved = tuple(item.reference for item in evidence)
        if not evidence:
            audit["stop_reason"] = "evidence_insufficient"
            return RagServiceResult(
                answer=self._answer(),
                retrieved=(),
                stop_reason="evidence_insufficient",
                audit=audit,
            )

        messages = build_grounded_messages(request.question, evidence)
        prompt_text = "\n".join(message.content for message in messages)
        try:
            targets = self.providers.targets()
        except RetryableProviderError as exc:
            audit["provider_attempts"] = [
                {"provider": "discovery", "outcome": "retryable_error", "error": type(exc).__name__}
            ]
            audit["stop_reason"] = "provider_error"
            return RagServiceResult(
                answer=self._answer(),
                retrieved=retrieved,
                stop_reason="provider_error",
                audit=audit,
            )
        except ProviderUnavailable as exc:
            audit["provider_attempts"] = [
                {"provider": "discovery", "outcome": "unavailable", "error": type(exc).__name__}
            ]
            audit["stop_reason"] = "no_model"
            return RagServiceResult(
                answer=self._answer(),
                retrieved=retrieved,
                stop_reason="no_model",
                audit=audit,
            )
        except ProviderCallError as exc:
            audit["provider_attempts"] = [
                {
                    "provider": "discovery",
                    "outcome": "non_retryable_error",
                    "error": type(exc).__name__,
                    "status_code": exc.status_code,
                }
            ]
            audit["stop_reason"] = "provider_error"
            return RagServiceResult(
                answer=self._answer(),
                retrieved=retrieved,
                stop_reason="provider_error",
                audit=audit,
            )
        if not targets:
            audit["stop_reason"] = "no_model"
            return RagServiceResult(
                answer=self._answer(),
                retrieved=retrieved,
                stop_reason="no_model",
                audit=audit,
            )

        attempts: list[dict[str, object]] = [
            {"provider": "discovery", **item}
            for item in self.providers.discovery_errors
        ]
        audit["provider_attempts"] = attempts
        for target in targets:
            model_pricing = self.pricing.resolve(target.name, target.model)
            if model_pricing is None:
                attempts.append(
                    {
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "unknown_pricing",
                    }
                )
                audit["stop_reason"] = "unknown_pricing"
                return RagServiceResult(
                    answer=self._answer(provider=target.name, model=target.model),
                    retrieved=retrieved,
                    stop_reason="unknown_pricing",
                    audit=audit,
                )
            estimate = estimate_budget(
                prompt_text,
                max_output_tokens=request.max_output_tokens,
                pricing=model_pricing,
            )
            try:
                self.budget.authorize(estimate)
            except BudgetExceeded:
                attempts.append(
                    {
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "budget_exceeded",
                        "estimated_cost": estimate.estimated_cost,
                        "pricing_version": model_pricing.version,
                    }
                )
                audit["stop_reason"] = "budget_exceeded"
                return RagServiceResult(
                    answer=self._answer(provider=target.name, model=target.model),
                    retrieved=retrieved,
                    stop_reason="budget_exceeded",
                    audit=audit,
                )
            try:
                response = target.complete(
                    messages,
                    max_output_tokens=request.max_output_tokens,
                )
            except RetryableProviderError as exc:
                attempts.append(
                    {
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "retryable_error",
                        "status_code": exc.status_code,
                    }
                )
                continue
            except (ProviderUnavailable, ProviderCallError) as exc:
                attempts.append(
                    {
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "non_retryable_error",
                        "status_code": getattr(exc, "status_code", None),
                    }
                )
                audit["stop_reason"] = "provider_error"
                return RagServiceResult(
                    answer=self._answer(provider=target.name, model=target.model),
                    retrieved=retrieved,
                    stop_reason="provider_error",
                    audit=audit,
                )

            input_tokens = response.input_tokens or estimate.input_tokens
            output_tokens = response.output_tokens
            actual = self._actual_estimate(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                pricing_input=model_pricing.input_per_million,
                pricing_output=model_pricing.output_per_million,
            )
            try:
                self.budget.record(actual)
            except BudgetExceeded:
                # The preflight reservation used a larger output allowance, so
                # this is only possible if another concurrent request consumed
                # the shared ledger in the meantime.
                attempts.append(
                    {
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "budget_race",
                    }
                )
                audit["stop_reason"] = "budget_exceeded"
                return RagServiceResult(
                    answer=self._answer(provider=target.name, model=target.model),
                    retrieved=retrieved,
                    stop_reason="budget_exceeded",
                    audit=audit,
                )

            if not response.text.strip():
                attempts.append(
                    {
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "empty_completion",
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "actual_cost": actual.estimated_cost,
                        "pricing_version": model_pricing.version,
                    }
                )
                audit["stop_reason"] = "evidence_insufficient"
                return RagServiceResult(
                    answer=self._answer(
                        provider=target.name,
                        model=target.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                    retrieved=retrieved,
                    stop_reason="evidence_insufficient",
                    audit=audit,
                )

            still_current = self._evidence_is_current(evidence)
            citations = (
                validate_provider_citations(
                    response.text,
                    response.cited_ids,
                    evidence,
                )
                if still_current
                else None
            )
            attempts.append(
                {
                    "provider": target.name,
                    "model": target.model,
                    "outcome": (
                        "completed"
                        if citations is not None
                        else "invalid_citation_discarded"
                    ),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "actual_cost": actual.estimated_cost,
                    "pricing_version": model_pricing.version,
                }
            )
            if citations is None:
                # Provider text that cannot pass deterministic citation and
                # freshness checks is never exposed to the caller.  A safe
                # evidence-insufficient answer is the public outcome while
                # the audit trail retains the rejected-output reason.
                audit["stop_reason"] = "evidence_insufficient"
                return RagServiceResult(
                    answer=self._answer(
                        provider=target.name,
                        model=target.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                    retrieved=retrieved,
                    stop_reason="evidence_insufficient",
                    audit=audit,
                )
            if response.text.strip() == _INSUFFICIENT:
                audit["stop_reason"] = "evidence_insufficient"
                return RagServiceResult(
                    answer=self._answer(
                        provider=target.name,
                        model=target.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                    retrieved=retrieved,
                    stop_reason="evidence_insufficient",
                    audit=audit,
                )
            audit["stop_reason"] = "completed"
            return RagServiceResult(
                answer=self._answer(
                    text=response.text,
                    provider=target.name,
                    model=target.model,
                    citations=citations,
                    insufficient=False,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
                retrieved=retrieved,
                stop_reason="completed",
                audit=audit,
            )

        audit["stop_reason"] = "provider_error"
        return RagServiceResult(
            answer=self._answer(),
            retrieved=retrieved,
            stop_reason="provider_error",
            audit=audit,
        )
