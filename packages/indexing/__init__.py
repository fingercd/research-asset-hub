"""Meilisearch keyword search abstractions and index builders."""

from .builder import (
    DEFAULT_INDEX_VERSION,
    KEYWORD_INDEX_NAMES,
    IndexBuilder,
    IndexCatalog,
    KeywordBuildReport,
    PostgresIndexCatalog,
    RemoteIndexSuite,
)
from .code_search import CodeSearch
from .filters import (
    canonical_filters,
    matches_metadata,
    meilisearch_filter,
    metadata_filters,
)
from .fusion import reciprocal_rank_fusion
from .keyword import KeywordSearchResult, KeywordSearchService, SearchMode
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
from .models import (
    BackendHealth,
    BackendState,
    SearchDocument,
    SearchHit,
)

__all__ = [
    "BackendHealth",
    "BackendState",
    "CodeSearch",
    "DEFAULT_INDEX_VERSION",
    "HttpxJSONTransport",
    "InMemoryKeywordIndex",
    "IndexBuilder",
    "IndexCatalog",
    "JSONTransport",
    "KEYWORD_INDEX_NAMES",
    "KeywordBuildReport",
    "KeywordIndex",
    "KeywordSearchResult",
    "KeywordSearchService",
    "MeilisearchAdmin",
    "MeilisearchIndex",
    "PostgresIndexCatalog",
    "RemoteIndexSuite",
    "ResilientKeywordIndex",
    "SearchDocument",
    "SearchHit",
    "SearchMode",
    "canonical_filters",
    "matches_metadata",
    "meilisearch_filter",
    "metadata_filters",
    "reciprocal_rank_fusion",
    "tokenize",
]
