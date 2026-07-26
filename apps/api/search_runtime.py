"""Lazy, process-wide hybrid search backend wiring and model lifecycle."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

from apps.api.config import ApiSettings
from packages.indexing import (
    Embedder,
    HashingEmbedder,
    HybridSearchResult,
    HybridSearchService,
    ManagedModel,
    ModelRuntimeStatus,
    RemoteIndexSuite,
    Reranker,
    ResilientEmbedder,
    ResilientReranker,
    SearchMode,
    SentenceTransformerEmbedder,
    SentenceTransformerReranker,
)

_SEARCH_SERIAL_LOCK = threading.Lock()
_GPU_SEARCH_MODES = frozenset((SearchMode.HYBRID, SearchMode.SEMANTIC))


@dataclass(frozen=True, slots=True)
class SearchModelsSnapshot:
    """Non-loading model residency exposed by the API health endpoint."""

    state: Literal["cold", "warm"]
    device: str | None
    idle_seconds: float
    idle_for_seconds: float
    embedder: ModelRuntimeStatus
    reranker: ModelRuntimeStatus


class _ActivityTrackedSearchService:
    """Keep SearchRuntime activity accounting around every service search."""

    def __init__(
        self,
        delegate: HybridSearchService,
        activity: Callable[[], AbstractContextManager[None]],
    ) -> None:
        self._delegate = delegate
        self._activity = activity

    @property
    def embedder(self) -> Embedder | None:
        return self._delegate.embedder

    @property
    def reranker(self) -> Reranker | None:
        return self._delegate.reranker

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
        with self._activity():
            if SearchMode(mode) in _GPU_SEARCH_MODES:
                # Count lock waiters as active requests so the idle monitor cannot
                # unload either model while another request is using the shared GPU.
                with _SEARCH_SERIAL_LOCK:
                    return self._delegate.search(
                        query,
                        mode=mode,
                        filters=filters,
                        limit=limit,
                        candidate_limit=candidate_limit,
                        rerank=rerank,
                    )
            return self._delegate.search(
                query,
                mode=mode,
                filters=filters,
                limit=limit,
                candidate_limit=candidate_limit,
                rerank=rerank,
            )


class SearchRuntime:
    """Own remote indexes and release local BGE models after request inactivity."""

    def __init__(
        self,
        suite: RemoteIndexSuite,
        service: HybridSearchService,
        *,
        idle_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.suite = suite
        self._service = service
        self._idle_seconds = max(0.0, idle_seconds)
        self._clock = clock
        self._activity_condition = threading.Condition(threading.RLock())
        self._active_requests = 0
        self._last_activity = self._clock()
        self._closed = False
        self.service = _ActivityTrackedSearchService(service, self.activity)

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
        return cls(
            suite=suite,
            service=service,
            idle_seconds=settings.search_model_idle_seconds,
        )

    @property
    def idle_seconds(self) -> float:
        return self._idle_seconds

    @contextmanager
    def activity(self) -> Iterator[None]:
        """Prevent idle unloading for the full duration of a search request."""

        with self._activity_condition:
            if self._closed:
                raise RuntimeError("Search runtime is closed")
            self._active_requests += 1
            self._last_activity = self._clock()
        try:
            yield
        finally:
            with self._activity_condition:
                self._active_requests -= 1
                self._last_activity = self._clock()
                if self._active_requests == 0:
                    self._activity_condition.notify_all()

    @staticmethod
    def _model_status(model: object | None) -> ModelRuntimeStatus:
        if isinstance(model, ManagedModel):
            return model.runtime_status
        return ModelRuntimeStatus(state="cold", device=None)

    @property
    def search_models(self) -> SearchModelsSnapshot:
        """Snapshot model state without touching either model's loading path."""

        with self._activity_condition:
            idle_for_seconds = (
                0.0
                if self._active_requests
                else max(0.0, self._clock() - self._last_activity)
            )
        embedder = self._model_status(self._service.embedder)
        reranker = self._model_status(self._service.reranker)
        states = (embedder, reranker)
        devices = {
            status.device
            for status in states
            if status.state == "warm" and status.device is not None
        }
        if not devices:
            device: str | None = None
        elif len(devices) == 1:
            device = next(iter(devices))
        else:
            device = "mixed"
        return SearchModelsSnapshot(
            state="warm" if any(status.state == "warm" for status in states) else "cold",
            device=device,
            idle_seconds=self._idle_seconds,
            idle_for_seconds=idle_for_seconds,
            embedder=embedder,
            reranker=reranker,
        )

    def _unload_models(self) -> bool:
        released = False
        for model in (self._service.embedder, self._service.reranker):
            if isinstance(model, ManagedModel):
                released = model.unload() or released
        return released

    def unload_if_idle(self) -> bool:
        """Unload only when the configured interval elapsed with no active request."""

        with self._activity_condition:
            if self._closed or self._idle_seconds == 0 or self._active_requests:
                return False
            now = self._clock()
            if now - self._last_activity < self._idle_seconds:
                return False
            return self._unload_models()

    def close(self) -> None:
        """Drain active searches, unload models, and close remote transports once."""

        with self._activity_condition:
            if self._closed:
                return
            self._closed = True
            while self._active_requests:
                self._activity_condition.wait()
            self._unload_models()
        self.suite.close()
