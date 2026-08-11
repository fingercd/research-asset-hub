from __future__ import annotations

from apps.api.config import ApiSettings
from apps.api.search_runtime import SearchRuntime
from packages.indexing import KeywordSearchService


class _FakeSuite:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def search_service(self) -> KeywordSearchService:
        return KeywordSearchService({})


def test_settings_keep_meilisearch_only() -> None:
    settings = ApiSettings()
    assert settings.meilisearch_url == "http://127.0.0.1:7700"
    assert not hasattr(settings, "qdrant_url")
    assert not hasattr(settings, "embedding_model")
    assert not hasattr(settings, "reranker_model")


def test_runtime_close_closes_suite_once() -> None:
    suite = _FakeSuite()
    runtime = SearchRuntime(
        suite=suite,  # type: ignore[arg-type]
        service=KeywordSearchService({}),
    )

    runtime.close()
    runtime.close()

    assert suite.close_calls == 1
