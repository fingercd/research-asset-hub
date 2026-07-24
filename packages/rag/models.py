"""Provider-neutral RAG value objects.

The public objects in this module deliberately contain no database or HTTP
types.  API and worker processes can therefore pass already-retrieved
candidates into :class:`packages.rag.service.RagService` without coupling the
service to a particular search backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from packages.indexing.models import SearchHit


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query: str
    vector: tuple[float, ...] | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 8


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    hit: SearchHit
    citation_id: str


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    asset_id: str
    document_id: str
    representation_id: str | None
    anchor: dict[str, Any] | None
    title: str
    url: str
    chunk_id: str | None = None
    chunk_hash: str | None = None
    representation_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    cited_ids: tuple[str, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RAGAnswer:
    text: str
    citations: tuple[Citation, ...]
    provider: str
    model: str
    evidence_insufficient: bool
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class UnifiedCandidates:
    """Candidates collected by the four retrieval channels.

    Every sequence is expected to be ordered best-first.  The service fuses
    them with RRF (``k=60``), preserving exact matches supplied by a search
    backend.  Keeping retrieval outside the service makes structured SQL,
    Meilisearch, Qdrant and graph traversal independently replaceable.
    """

    structured: tuple[SearchHit, ...] = ()
    keyword: tuple[SearchHit, ...] = ()
    vector: tuple[SearchHit, ...] = ()
    graph: tuple[SearchHit, ...] = ()
    index_versions: dict[str, str] = field(default_factory=dict)

    def result_sets(self) -> tuple[tuple[SearchHit, ...], ...]:
        return tuple(
            values
            for values in (
                self.structured,
                self.keyword,
                self.vector,
                self.graph,
            )
            if values
        )


@dataclass(frozen=True, slots=True)
class RagRequest:
    question: str
    candidates: UnifiedCandidates
    filters: dict[str, Any] = field(default_factory=dict)
    max_evidence: int = 8
    max_output_tokens: int = 1_000


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A safe retrieval snapshot.

    It intentionally omits the private chunk body.  The body is used only
    while building the provider request.
    """

    citation_id: str
    asset_id: str
    document_id: str
    representation_id: str
    chunk_id: str
    chunk_hash: str
    representation_digest: str
    anchor: dict[str, Any]
    score: float
    sources: tuple[str, ...]
    title: str
    url: str


StopReason = Literal[
    "completed",
    "evidence_insufficient",
    "no_model",
    "unknown_pricing",
    "budget_exceeded",
    "provider_error",
    "invalid_citation",
]


@dataclass(frozen=True, slots=True)
class RagServiceResult:
    answer: RAGAnswer
    retrieved: tuple[EvidenceReference, ...]
    stop_reason: StopReason
    audit: dict[str, Any]
