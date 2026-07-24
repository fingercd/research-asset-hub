from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from packages.graph import (
    GraphQueries,
    InMemoryRelationStore,
    PostgresRelationStore,
    RelationCandidateRecord,
    RelationEdge,
    deduplicate_candidates,
)
from packages.indexing import InMemoryKeywordIndex, SearchDocument
from packages.rag import (
    BudgetExceeded,
    BudgetLedger,
    BudgetPolicy,
    ChatMessage,
    DeepSeekAdapter,
    HybridRetriever,
    MiniMaxAdapter,
    ProviderResponse,
    ProviderUnavailable,
    RAGAnsweringService,
    RetrievalRequest,
)


def edge(edge_id: str, subject: str, object_id: str) -> RelationEdge:
    return RelationEdge(edge_id, subject, object_id, "related_to")


class FakePostgres:
    def __init__(self) -> None:
        self.query = ""
        self.params: Mapping[str, Any] = {}

    def fetch_all(
        self,
        query: str,
        params: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        self.query = query
        self.params = params
        return [
            {
                "id": "e1",
                "subject_asset_id": "a",
                "object_asset_id": "b",
                "relation_type": "cites",
                "confidence": 0.9,
                "confirmation_status": "confirmed",
                "evidence": {"source": "fixture"},
            }
        ]


class CitingProvider:
    name = "fixture"
    model = "fixture-model"

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int,
    ) -> ProviderResponse:
        assert "[citation:chunk-1]" in messages[-1].content
        return ProviderResponse(
            text="The method uses planning. [citation:chunk-1]",
            cited_ids=("chunk-1",),
            input_tokens=20,
            output_tokens=10,
        )


def test_graph_shortest_path_common_neighbors_and_postgres_query() -> None:
    graph = GraphQueries(
        InMemoryRelationStore(
            [
                edge("e1", "a", "b"),
                edge("e2", "b", "c"),
                edge("e3", "d", "b"),
            ]
        )
    )
    path = graph.shortest_path("a", "c")
    assert path is not None
    assert path.nodes == ("a", "b", "c")
    assert graph.common_neighbors("a", "d") == ["b"]

    executor = FakePostgres()
    rows = PostgresRelationStore(executor).edges_for(["a"], relation_types=["cites"])
    assert rows[0].confirmation_status == "confirmed"
    assert "ANY(%(node_ids)s::uuid[])" in executor.query
    assert executor.params["relation_types"] == ["cites"]


def test_relation_candidates_deduplicate_by_evidenced_identity() -> None:
    values = deduplicate_candidates(
        [
            RelationCandidateRecord("a", "cites", "b", "provider", 0.4),
            RelationCandidateRecord("a", "cites", "b", "provider", 0.9),
        ]
    )
    assert len(values) == 1
    assert values[0].confidence == 0.9


def test_rag_requires_openable_citations_and_enforces_budget() -> None:
    keyword = InMemoryKeywordIndex()
    keyword.upsert(
        [
            SearchDocument(
                document_id="chunk-1",
                asset_id="paper-1",
                kind="document",
                title="Planning",
                text="The method uses planning in latent space.",
                metadata={
                    "representation_id": "pdf-1",
                    "anchor": {"kind": "pdf", "page": 3},
                },
            )
        ]
    )
    service = RAGAnsweringService(
        HybridRetriever(keyword),
        CitingProvider(),
        budget=BudgetLedger(BudgetPolicy(max_request_tokens=10_000)),
    )
    answer = service.answer(RetrievalRequest("How does the method plan?"))
    assert answer.evidence_insufficient is False
    assert answer.citations[0].anchor == {"kind": "pdf", "page": 3}
    assert answer.citations[0].url.startswith("/assets/paper-1/")

    too_small = RAGAnsweringService(
        HybridRetriever(keyword),
        CitingProvider(),
        budget=BudgetLedger(BudgetPolicy(max_request_tokens=1)),
    )
    with pytest.raises(BudgetExceeded):
        too_small.answer(RetrievalRequest("How does the method plan?"))


def test_provider_adapters_do_not_require_real_key_at_construction() -> None:
    providers = [
        DeepSeekAdapter(
            endpoint="https://invalid.local/chat/completions",
            model="deepseek-test",
        ),
        MiniMaxAdapter(
            endpoint="https://invalid.local/chat/completions",
            model="minimax-test",
        ),
    ]
    for provider in providers:
        with pytest.raises(ProviderUnavailable):
            provider.complete([ChatMessage("user", "test")], max_output_tokens=5)
