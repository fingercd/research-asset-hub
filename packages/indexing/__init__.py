"""Search backend abstractions, fallbacks and fusion."""

from .builder import (
    DEFAULT_VECTOR_VERSION,
    KEYWORD_INDEX_NAMES,
    IndexBuilder,
    IndexBuildReport,
    IndexCatalog,
    PostgresIndexCatalog,
    RemoteIndexSuite,
    degraded_hashing_embedder,
)
from .code_search import CodeSearch
from .embeddings import (
    Embedder,
    EmbeddingStatus,
    HashingEmbedder,
    ResilientEmbedder,
    SentenceTransformerEmbedder,
)
from .filters import (
    canonical_filters,
    matches_metadata,
    meilisearch_filter,
    metadata_filters,
    qdrant_filter,
)
from .fusion import apply_score_boosts, reciprocal_rank_fusion
from .hybrid import HybridSearchResult, HybridSearchService, SearchMode
from .meili import (
    HttpxJSONTransport,
    InMemoryKeywordIndex,
    JSONTransport,
    KeywordIndex,
    MeilisearchAdmin,
    MeilisearchIndex,
    ResilientKeywordIndex,
    tokenize,
)
from .model_lifecycle import ManagedModel, ModelRuntimeStatus
from .models import (
    BackendHealth,
    BackendState,
    SearchDocument,
    SearchHit,
    VectorPoint,
)
from .qdrant import (
    InMemoryVectorIndex,
    QdrantAdmin,
    QdrantIndex,
    ResilientVectorIndex,
    VectorIndex,
    cosine_similarity,
)
from .rerank import (
    LexicalReranker,
    Reranker,
    RerankerStatus,
    ResilientReranker,
    SentenceTransformerReranker,
)
from .similarity import SimilarPaperFinder

__all__ = [
    "BackendHealth",
    "BackendState",
    "CodeSearch",
    "DEFAULT_VECTOR_VERSION",
    "Embedder",
    "EmbeddingStatus",
    "HashingEmbedder",
    "HybridSearchResult",
    "HybridSearchService",
    "HttpxJSONTransport",
    "InMemoryKeywordIndex",
    "InMemoryVectorIndex",
    "IndexBuildReport",
    "IndexBuilder",
    "IndexCatalog",
    "JSONTransport",
    "KEYWORD_INDEX_NAMES",
    "KeywordIndex",
    "LexicalReranker",
    "MeilisearchAdmin",
    "MeilisearchIndex",
    "ManagedModel",
    "ModelRuntimeStatus",
    "PostgresIndexCatalog",
    "QdrantAdmin",
    "QdrantIndex",
    "RemoteIndexSuite",
    "Reranker",
    "RerankerStatus",
    "ResilientEmbedder",
    "ResilientKeywordIndex",
    "ResilientReranker",
    "ResilientVectorIndex",
    "SearchDocument",
    "SearchHit",
    "SentenceTransformerEmbedder",
    "SentenceTransformerReranker",
    "SimilarPaperFinder",
    "SearchMode",
    "VectorIndex",
    "VectorPoint",
    "apply_score_boosts",
    "canonical_filters",
    "cosine_similarity",
    "degraded_hashing_embedder",
    "matches_metadata",
    "meilisearch_filter",
    "metadata_filters",
    "qdrant_filter",
    "reciprocal_rank_fusion",
    "tokenize",
]
