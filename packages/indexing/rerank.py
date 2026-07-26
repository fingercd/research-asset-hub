"""Optional local BGE reranking with a deterministic lexical fallback."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .meili import tokenize
from .model_lifecycle import ManagedModel, ModelRuntimeStatus, release_model_memory
from .models import SearchHit

MAX_RERANK_TEXT_CHARS = 32_768


@dataclass(frozen=True, slots=True)
class RerankerStatus:
    provider: str
    model: str
    available: bool
    detail: str | None = None


class Reranker(Protocol):
    @property
    def status(self) -> RerankerStatus: ...

    def rerank(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        limit: int,
    ) -> list[SearchHit]: ...


class LexicalReranker:
    @property
    def status(self) -> RerankerStatus:
        return RerankerStatus(
            provider="local-lexical",
            model="token-overlap-v1",
            available=True,
            detail="degraded=true; BGE reranker is not loaded",
        )

    def rerank(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        limit: int,
    ) -> list[SearchHit]:
        query_terms = set(tokenize(query))
        scored: list[tuple[float, SearchHit]] = []
        for hit in hits:
            document_terms = set(tokenize(f"{hit.document.title}\n{hit.document.text}"))
            overlap = len(query_terms.intersection(document_terms))
            score = hit.score + overlap * 0.01
            scored.append((score, hit))
        scored.sort(
            key=lambda item: (
                not item[1].exact_match,
                -item[0],
                item[1].document.document_id,
            )
        )
        return [
            replace(hit, score=score, rank=rank)
            for rank, (score, hit) in enumerate(scored[:limit], start=1)
        ]


class SentenceTransformerReranker:
    """Lazy ``BAAI/bge-reranker-v2-m3`` cross-encoder."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        *,
        device: str | None = None,
        batch_size: int = 4,
        max_length: int = 512,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self.batch_size = batch_size
        self.max_length = max(64, max_length)
        self._model: Any | None = None
        self._device: str | None = None
        self._load_error: str | None = None
        self._operation_lock = threading.RLock()
        self._state_lock = threading.Lock()

    def _load(self) -> Any:
        with self._operation_lock:
            with self._state_lock:
                if self._model is not None:
                    return self._model
            try:
                sentence_transformers: Any = importlib.import_module("sentence_transformers")
                torch: Any = importlib.import_module("torch")
                device = self.requested_device or ("cuda" if torch.cuda.is_available() else "cpu")
                try:
                    model = sentence_transformers.CrossEncoder(
                        self.model_name,
                        device=device,
                        max_length=self.max_length,
                        trust_remote_code=False,
                    )
                except Exception:
                    if device == "cpu":
                        raise
                    device = "cpu"
                    model = sentence_transformers.CrossEncoder(
                        self.model_name,
                        device=device,
                        max_length=self.max_length,
                        trust_remote_code=False,
                    )
            except Exception as error:
                load_error = f"{type(error).__name__}: {error}"
                with self._state_lock:
                    self._load_error = load_error
                raise RuntimeError(f"Unable to load reranker: {load_error}") from error
            with self._state_lock:
                self._model = model
                self._device = device
                self._load_error = None
            return model

    @property
    def runtime_status(self) -> ModelRuntimeStatus:
        """Return model residency without importing or loading the model."""

        with self._state_lock:
            return ModelRuntimeStatus(
                state="warm" if self._model is not None else "cold",
                device=self._device if self._model is not None else None,
            )

    def unload(self) -> bool:
        """Release the loaded model after all in-flight inference has completed."""

        with self._operation_lock:
            with self._state_lock:
                was_loaded = self._model is not None
                device = self._device
                self._model = None
                self._device = None
                self._load_error = None
            if was_loaded:
                release_model_memory(device)
            return was_loaded

    @property
    def status(self) -> RerankerStatus:
        with self._operation_lock:
            try:
                self._load()
            except RuntimeError:
                with self._state_lock:
                    load_error = self._load_error
                return RerankerStatus(
                    provider="sentence-transformers",
                    model=self.model_name,
                    available=False,
                    detail=load_error,
                )
            with self._state_lock:
                device = self._device
            return RerankerStatus(
                provider="sentence-transformers",
                model=self.model_name,
                available=True,
                detail=f"device={device}; max_length={self.max_length}",
            )

    def rerank(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        limit: int,
    ) -> list[SearchHit]:
        if not hits:
            return []
        with self._operation_lock:
            model = self._load()
            pairs = [
                (
                    query,
                    f"{hit.document.title}\n{hit.document.text[:MAX_RERANK_TEXT_CHARS]}",
                )
                for hit in hits
            ]
            batch_size = max(1, self.batch_size)
            while True:
                try:
                    raw_scores: Any = model.predict(
                        pairs,
                        batch_size=batch_size,
                        show_progress_bar=False,
                    )
                    break
                except RuntimeError:
                    if batch_size > 1:
                        batch_size = max(1, batch_size // 2)
                        continue
                    with self._state_lock:
                        device = self._device
                    if device == "cuda":
                        model.model = model.model.to("cpu")
                        with self._state_lock:
                            self._device = "cpu"
                        continue
                    raise
            scores = [float(item) for item in raw_scores.tolist()]
            ranked = list(zip(scores, hits, strict=True))
            ranked.sort(
                key=lambda item: (
                    not item[1].exact_match,
                    -item[0],
                    item[1].document.document_id,
                )
            )
            return [
                replace(hit, score=score, rank=rank, source=f"{hit.source}+bge-reranker")
                for rank, (score, hit) in enumerate(ranked[:limit], start=1)
            ]


class ResilientReranker:
    def __init__(
        self,
        primary: Reranker | None = None,
        fallback: Reranker | None = None,
    ) -> None:
        self.primary = primary or SentenceTransformerReranker()
        self.fallback = fallback or LexicalReranker()
        self._active: Reranker | None = None
        self._error: str | None = None
        self._operation_lock = threading.RLock()

    @property
    def runtime_status(self) -> ModelRuntimeStatus:
        """Report only the primary BGE model's residency without loading it."""

        if isinstance(self.primary, ManagedModel):
            return self.primary.runtime_status
        return ModelRuntimeStatus(state="cold", device=None)

    def unload(self) -> bool:
        """Release managed backends and retry the primary model on the next query."""

        with self._operation_lock:
            was_active = self._active is not None
            released = False
            for reranker in (self.primary, self.fallback):
                if isinstance(reranker, ManagedModel):
                    released = reranker.unload() or released
            self._active = None
            self._error = None
            return released or was_active

    @property
    def status(self) -> RerankerStatus:
        with self._operation_lock:
            if self._active is self.fallback:
                fallback = self.fallback.status
                return replace(
                    fallback,
                    detail=f"degraded=true; primary_error={self._error}",
                )
            if self._active is self.primary:
                return self.primary.status
            primary_status = self.primary.status
            return primary_status if primary_status.available else self.fallback.status

    def rerank(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        limit: int,
    ) -> list[SearchHit]:
        with self._operation_lock:
            if self._active is not None:
                return self._active.rerank(query, hits, limit=limit)
            try:
                ranked = self.primary.rerank(query, hits, limit=limit)
            except Exception as error:
                self._error = f"{type(error).__name__}: {error}"
                self._active = self.fallback
                return self.fallback.rerank(query, hits, limit=limit)
            self._active = self.primary
            return ranked
