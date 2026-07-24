"""Evidence-gated RAG answering service."""

from __future__ import annotations

from .budget import BudgetLedger, ModelPricing, estimate_budget
from .citations import select_citations
from .models import RAGAnswer, RetrievalRequest
from .providers import ChatMessage, ChatProvider
from .rerank import lexical_rerank
from .retrieval import HybridRetriever

_INSUFFICIENT = "资料库证据不足"


class RAGAnsweringService:
    def __init__(
        self,
        retriever: HybridRetriever,
        provider: ChatProvider,
        *,
        budget: BudgetLedger | None = None,
        pricing: ModelPricing | None = None,
        max_output_tokens: int = 1_000,
    ) -> None:
        self.retriever = retriever
        self.provider = provider
        self.budget = budget or BudgetLedger()
        self.pricing = pricing or ModelPricing()
        self.max_output_tokens = max_output_tokens

    def answer(self, request: RetrievalRequest) -> RAGAnswer:
        evidence = lexical_rerank(
            request.query,
            self.retriever.retrieve(request),
        )
        if not evidence:
            return RAGAnswer(
                text=_INSUFFICIENT,
                citations=(),
                provider=self.provider.name,
                model=self.provider.model,
                evidence_insufficient=True,
            )
        context = "\n\n".join(
            (
                f"[citation:{item.citation_id}] "
                f"{item.hit.document.title}\n{item.hit.document.text}"
            )
            for item in evidence
        )
        prompt = (
            "仅依据下列资料回答。每个事实后必须写出对应的 "
            "[citation:<id>]；若证据不足，只回答“资料库证据不足”。\n\n"
            f"资料：\n{context}\n\n问题：{request.query}"
        )
        estimate = estimate_budget(
            prompt,
            max_output_tokens=self.max_output_tokens,
            pricing=self.pricing,
        )
        self.budget.authorize(estimate)
        response = self.provider.complete(
            [
                ChatMessage("system", "You answer only from supplied evidence."),
                ChatMessage("user", prompt),
            ],
            max_output_tokens=self.max_output_tokens,
        )
        if not response.cited_ids:
            return RAGAnswer(
                text=_INSUFFICIENT,
                citations=(),
                provider=self.provider.name,
                model=self.provider.model,
                evidence_insufficient=True,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        try:
            citations = select_citations(response.cited_ids, evidence)
        except ValueError:
            return RAGAnswer(
                text=_INSUFFICIENT,
                citations=(),
                provider=self.provider.name,
                model=self.provider.model,
                evidence_insufficient=True,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        actual = estimate_budget(
            prompt,
            max_output_tokens=max(response.output_tokens, 1),
            pricing=self.pricing,
        )
        self.budget.record(actual)
        return RAGAnswer(
            text=response.text,
            citations=citations,
            provider=self.provider.name,
            model=self.provider.model,
            evidence_insufficient=False,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
