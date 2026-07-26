from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from time import monotonic
from typing import Any, cast

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.main import _monitor_search_models
from apps.api.rag_runtime import retrieve_candidates
from apps.api.search_runtime import SearchRuntime, _ActivityTrackedSearchService
from packages.domain.models import Base
from packages.indexing import (
    EmbeddingStatus,
    HybridSearchResult,
    HybridSearchService,
    ModelRuntimeStatus,
    RemoteIndexSuite,
    RerankerStatus,
    SearchHit,
    SearchMode,
)


class _KeywordIndex:
    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        return []


class _ChunkVectors:
    def __init__(self) -> None:
        self.search_calls = 0

    def search(self, vector: list[float], *, limit: int) -> list[SearchHit]:
        self.search_calls += 1
        return []


class _FakeSuite:
    def __init__(self) -> None:
        self.close_calls = 0
        self.keyword_indexes = {
            name: _KeywordIndex() for name in ("documents", "code", "annotations")
        }
        self.chunk_vectors = _ChunkVectors()
        self.chunk_collection = "test_chunks"

    def close(self) -> None:
        self.close_calls += 1


class _ManagedEmbedder:
    def __init__(self, *, device: str = "cuda", fail_encode: bool = False) -> None:
        self.loaded = True
        self.device = device
        self.fail_encode = fail_encode
        self.encode_calls = 0
        self.unload_calls = 0

    @property
    def status(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            provider="sentence-transformers",
            model="BAAI/bge-m3",
            dimension=1,
            available=True,
        )

    @property
    def runtime_status(self) -> ModelRuntimeStatus:
        return ModelRuntimeStatus(
            state="warm" if self.loaded else "cold",
            device=self.device if self.loaded else None,
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encode_calls += 1
        if self.fail_encode:
            raise RuntimeError("test embedding failure")
        return [[1.0] for _ in texts]

    def unload(self) -> bool:
        self.unload_calls += 1
        was_loaded = self.loaded
        self.loaded = False
        return was_loaded


class _ManagedReranker:
    def __init__(self, *, device: str = "cpu") -> None:
        self.loaded = True
        self.device = device
        self.unload_calls = 0

    @property
    def status(self) -> RerankerStatus:
        return RerankerStatus(
            provider="sentence-transformers",
            model="BAAI/bge-reranker-v2-m3",
            available=True,
        )

    @property
    def runtime_status(self) -> ModelRuntimeStatus:
        return ModelRuntimeStatus(
            state="warm" if self.loaded else "cold",
            device=self.device if self.loaded else None,
        )

    def rerank(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        limit: int,
    ) -> list[SearchHit]:
        return list(hits[:limit])

    def unload(self) -> bool:
        self.unload_calls += 1
        was_loaded = self.loaded
        self.loaded = False
        return was_loaded


class _SerialProbeSearchService(HybridSearchService):
    def __init__(self) -> None:
        super().__init__({})
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self._state_lock = threading.Lock()
        self.active_delegate_calls = 0
        self.max_delegate_calls = 0
        self.active_gpu_delegate_calls = 0
        self.max_gpu_delegate_calls = 0
        self.entered_queries: list[str] = []

    def search(
        self,
        query: str,
        *,
        mode: SearchMode | str = SearchMode.HYBRID,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        candidate_limit: int | None = None,
        rerank: bool = True,
    ) -> HybridSearchResult:
        del filters, limit, candidate_limit, rerank
        selected_mode = SearchMode(mode)
        uses_gpu = selected_mode in {SearchMode.HYBRID, SearchMode.SEMANTIC}
        with self._state_lock:
            self.active_delegate_calls += 1
            self.max_delegate_calls = max(
                self.max_delegate_calls,
                self.active_delegate_calls,
            )
            if uses_gpu:
                self.active_gpu_delegate_calls += 1
                self.max_gpu_delegate_calls = max(
                    self.max_gpu_delegate_calls,
                    self.active_gpu_delegate_calls,
                )
            self.entered_queries.append(query)
        try:
            if query == "first":
                self.first_entered.set()
                if not self.release_first.wait(timeout=5):
                    raise AssertionError("first search was not released")
            return HybridSearchResult(
                hits=(),
                mode=selected_mode,
                degraded=False,
                keyword_health=(),
                vector_health=(),
                embedding_status=None,
                reranker_status=None,
            )
        finally:
            with self._state_lock:
                self.active_delegate_calls -= 1
                if uses_gpu:
                    self.active_gpu_delegate_calls -= 1


def _runtime(
    *,
    idle_seconds: float,
    clock: list[float] | None = None,
    fail_encode: bool = False,
) -> tuple[SearchRuntime, _FakeSuite, _ManagedEmbedder, _ManagedReranker]:
    suite = _FakeSuite()
    embedder = _ManagedEmbedder(fail_encode=fail_encode)
    reranker = _ManagedReranker()
    service = HybridSearchService(
        {},
        embedder=embedder,
        reranker=reranker,
    )
    runtime = SearchRuntime(
        cast(RemoteIndexSuite, suite),
        service,
        idle_seconds=idle_seconds,
        clock=(lambda: clock[0]) if clock is not None else monotonic,
    )
    return runtime, suite, embedder, reranker


def test_service_serializes_gpu_search_but_exact_bypasses_the_gpu_lock() -> None:
    delegate = _SerialProbeSearchService()
    activity_lock = threading.Lock()
    both_requests_active = threading.Event()
    active_requests = 0

    @contextmanager
    def activity() -> Iterator[None]:
        nonlocal active_requests
        with activity_lock:
            active_requests += 1
            if active_requests == 2:
                both_requests_active.set()
        try:
            yield
        finally:
            with activity_lock:
                active_requests -= 1

    service = _ActivityTrackedSearchService(delegate, activity)
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(service.search, "first", mode=SearchMode.HYBRID)
        assert delegate.first_entered.wait(timeout=2)
        second = executor.submit(service.search, "second", mode=SearchMode.SEMANTIC)
        try:
            assert both_requests_active.wait(timeout=2)
            exact = executor.submit(service.search, "exact", mode=SearchMode.EXACT)
            assert exact.result(timeout=2).mode == SearchMode.EXACT
        finally:
            delegate.release_first.set()

        assert first.result(timeout=2).mode == SearchMode.HYBRID
        assert second.result(timeout=2).mode == SearchMode.SEMANTIC

    assert delegate.max_gpu_delegate_calls == 1
    assert delegate.max_delegate_calls == 2
    assert delegate.entered_queries == ["first", "exact", "second"]
    assert active_requests == 0


def test_idle_setting_defaults_to_900_and_zero_is_allowed() -> None:
    assert ApiSettings.model_fields["search_model_idle_seconds"].default == 900.0
    assert ApiSettings(search_model_idle_seconds=0).search_model_idle_seconds == 0


def test_idle_setting_reads_hub_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_SEARCH_MODEL_IDLE_SECONDS", "17")

    assert ApiSettings().search_model_idle_seconds == 17


def test_runtime_tracks_search_activity_and_unloads_only_when_idle() -> None:
    now = [0.0]
    runtime, suite, embedder, reranker = _runtime(idle_seconds=10, clock=now)

    initial = runtime.search_models
    assert initial.state == "warm"
    assert initial.device == "mixed"
    assert initial.idle_for_seconds == 0
    assert initial.embedder.device == "cuda"
    assert initial.reranker.device == "cpu"

    now[0] = 5.0
    runtime.service.search("", mode="exact")
    now[0] = 14.9
    assert runtime.search_models.idle_for_seconds == pytest.approx(9.9)
    assert not runtime.unload_if_idle()

    now[0] = 100.0
    with runtime.activity():
        assert runtime.search_models.idle_for_seconds == 0
        assert not runtime.unload_if_idle()
    now[0] = 110.0
    assert runtime.unload_if_idle()
    assert embedder.unload_calls == 1
    assert reranker.unload_calls == 1
    assert runtime.search_models.state == "cold"
    assert runtime.search_models.idle_for_seconds == 10
    now[0] = 120.0
    assert runtime.search_models.idle_for_seconds == 20

    runtime.close()
    assert suite.close_calls == 1


def test_service_search_exception_releases_activity_for_idle_unload() -> None:
    now = [0.0]
    runtime, suite, embedder, reranker = _runtime(idle_seconds=10, clock=now)

    now[0] = 5.0
    with pytest.raises(ValueError):
        runtime.service.search("query", mode="invalid")

    now[0] = 6.0
    assert runtime.search_models.idle_for_seconds == 1
    now[0] = 15.0
    assert runtime.unload_if_idle()
    assert not embedder.loaded
    assert not reranker.loaded

    runtime.close()
    assert suite.close_calls == 1


def test_rag_vector_exception_releases_activity_for_idle_unload() -> None:
    now = [5.0]
    runtime, suite, embedder, reranker = _runtime(
        idle_seconds=10,
        clock=now,
        fail_encode=True,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            candidates, digests = retrieve_candidates(
                session,
                "vector question",
                search_runtime=runtime,
            )
        assert candidates.vector == ()
        assert digests == {}
        assert embedder.encode_calls == 1
        assert suite.chunk_vectors.search_calls == 0

        now[0] = 6.0
        assert runtime.search_models.idle_for_seconds == 1
        now[0] = 15.0
        assert runtime.unload_if_idle()
        assert not embedder.loaded
        assert not reranker.loaded
    finally:
        runtime.close()
        engine.dispose()

    assert suite.close_calls == 1


def test_zero_idle_seconds_disables_background_unload() -> None:
    now = [0.0]
    runtime, suite, embedder, reranker = _runtime(idle_seconds=0, clock=now)

    now[0] = 86_400.0
    assert not runtime.unload_if_idle()
    assert embedder.loaded
    assert reranker.loaded

    runtime.close()
    assert suite.close_calls == 1


@pytest.mark.asyncio
async def test_lifespan_monitor_checks_runtime_for_idle_models() -> None:
    runtime, suite, embedder, reranker = _runtime(idle_seconds=0.05)
    application = FastAPI()
    application.state.search_runtime = runtime
    monitor = asyncio.create_task(_monitor_search_models(application, 0.05))
    try:
        for _ in range(20):
            if not embedder.loaded and not reranker.loaded:
                break
            await asyncio.sleep(0.02)
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor

    assert not embedder.loaded
    assert not reranker.loaded
    runtime.close()
    assert suite.close_calls == 1
