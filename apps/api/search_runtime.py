"""Process-wide Meilisearch keyword search backend."""

from __future__ import annotations

from apps.api.config import ApiSettings
from packages.indexing import KeywordSearchService, RemoteIndexSuite
from packages.indexing.builder import DEFAULT_REFRESH_INDEX_NAMES


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
            for name in (*DEFAULT_REFRESH_INDEX_NAMES, "code")
            if name != "code" or settings.search_code_index_enabled
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
