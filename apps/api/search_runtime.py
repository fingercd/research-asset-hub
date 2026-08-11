"""Process-wide Meilisearch keyword search backend."""

from __future__ import annotations

from apps.api.config import ApiSettings
from packages.indexing import KeywordSearchService, RemoteIndexSuite


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
        suite = RemoteIndexSuite.connect(
            meilisearch_url=settings.meilisearch_url,
            meilisearch_api_key=settings.meilisearch_api_key,
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
