"""Process-wide Meilisearch keyword search backend."""

from __future__ import annotations

from apps.api.config import ApiSettings
from packages.indexing import KEYWORD_INDEX_NAMES, KeywordSearchService, RemoteIndexSuite


class SearchRuntime:
    """Own the Meilisearch keyword indexes for the API process."""

    def __init__(
        self,
        suite: RemoteIndexSuite,
        service: KeywordSearchService,
    ) -> None:
        self.suite = suite
        self.service = service
        self._closed = False

    @classmethod
    def connect(cls, settings: ApiSettings) -> SearchRuntime:
        index_names = tuple(
            name
            for name in KEYWORD_INDEX_NAMES
            if settings.search_code_index_enabled or name != "code"
        )
        suite = RemoteIndexSuite.connect(
            meilisearch_url=settings.meilisearch_url,
            meilisearch_api_key=settings.meilisearch_api_key,
            index_names=index_names,
            initialize_indexes=False,
            request_timeout=3,
        )
        return cls(
            suite=suite,
            service=suite.search_service(),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.suite.close()
