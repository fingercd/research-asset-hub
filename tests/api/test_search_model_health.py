from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.api.health import (
    HealthResponse,
    SearchModelHealth,
    SearchModelsHealth,
)
from apps.api.search_runtime import SearchRuntime
from packages.indexing import (
    HashingEmbedder,
    HybridSearchService,
    RemoteIndexSuite,
    ResilientEmbedder,
    ResilientReranker,
    SentenceTransformerEmbedder,
    SentenceTransformerReranker,
)


class _FakeSuite:
    def close(self) -> None:
        return None


def test_health_reports_cold_models_without_loading_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        search_model_idle_seconds=123,
    )
    database = DatabaseManager(settings)
    embedder = SentenceTransformerEmbedder(dimension=1024)
    reranker = SentenceTransformerReranker()
    service = HybridSearchService(
        {},
        embedder=ResilientEmbedder(embedder, HashingEmbedder(1024)),
        reranker=ResilientReranker(reranker),
    )
    runtime = SearchRuntime(
        cast(RemoteIndexSuite, _FakeSuite()),
        service,
        idle_seconds=settings.search_model_idle_seconds,
        clock=lambda: 42.0,
    )
    load_attempts: list[str] = []

    def unexpected_embedding_load() -> Any:
        load_attempts.append("embedder")
        raise AssertionError("health check loaded the embedder")

    def unexpected_reranker_load() -> Any:
        load_attempts.append("reranker")
        raise AssertionError("health check loaded the reranker")

    monkeypatch.setattr(embedder, "_load", unexpected_embedding_load)
    monkeypatch.setattr(reranker, "_load", unexpected_reranker_load)

    async def fake_collect_health(_: ApiSettings) -> HealthResponse:
        return HealthResponse(
            status="healthy",
            checked_at=datetime.now(UTC),
            components={},
            search_models=SearchModelsHealth(
                state="cold",
                idle_seconds=settings.search_model_idle_seconds,
                idle_for_seconds=0,
                embedder=SearchModelHealth(state="cold"),
                reranker=SearchModelHealth(state="cold"),
            ),
        )

    monkeypatch.setattr(api_main, "collect_health", fake_collect_health)
    application = api_main.create_app(settings, database=database)
    application.state.search_runtime = runtime

    try:
        with TestClient(application) as client:
            response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["search_models"] == {
            "state": "cold",
            "device": None,
            "idle_seconds": 123.0,
            "idle_for_seconds": 0.0,
            "embedder": {"state": "cold", "device": None},
            "reranker": {"state": "cold", "device": None},
        }
        assert load_attempts == []
    finally:
        database.dispose()
