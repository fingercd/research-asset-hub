"""Lazy, process-wide hybrid search backend wiring."""

from __future__ import annotations

from dataclasses import dataclass

from apps.api.config import ApiSettings
from packages.indexing import (
    HashingEmbedder,
    HybridSearchService,
    RemoteIndexSuite,
    ResilientEmbedder,
    ResilientReranker,
    SentenceTransformerEmbedder,
    SentenceTransformerReranker,
)


@dataclass(slots=True)
class SearchRuntime:
    suite: RemoteIndexSuite
    service: HybridSearchService

    @classmethod
    def connect(cls, settings: ApiSettings) -> SearchRuntime:
        suite = RemoteIndexSuite.connect(
            meilisearch_url=settings.meilisearch_url,
            meilisearch_api_key=settings.meilisearch_api_key,
            qdrant_url=settings.qdrant_url,
            dimension=1024,
            version=settings.search_vector_version,
        )
        service = suite.search_service(
            embedder=ResilientEmbedder(
                SentenceTransformerEmbedder(
                    settings.embedding_model,
                    dimension=1024,
                    batch_size=settings.embedding_batch_size,
                    max_sequence_length=settings.embedding_max_sequence_length,
                    use_half_precision=settings.embedding_half_precision,
                ),
                HashingEmbedder(1024),
            ),
            reranker=ResilientReranker(
                SentenceTransformerReranker(
                    settings.reranker_model,
                    batch_size=settings.reranker_batch_size,
                    max_length=settings.reranker_max_sequence_length,
                )
            ),
        )
        return cls(suite=suite, service=service)

    def close(self) -> None:
        self.suite.close()
