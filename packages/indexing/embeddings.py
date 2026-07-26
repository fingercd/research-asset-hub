"""Local embedding providers with a deterministic offline fallback."""

from __future__ import annotations

import hashlib
import importlib
import math
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from .model_lifecycle import ManagedModel, ModelRuntimeStatus, release_model_memory


@dataclass(frozen=True, slots=True)
class EmbeddingStatus:
    provider: str
    model: str
    dimension: int
    available: bool
    detail: str | None = None


class Embedder(Protocol):
    @property
    def status(self) -> EmbeddingStatus: ...

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic degraded-mode vectors; not a semantic model."""

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension

    @property
    def status(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            provider="local-hashing",
            model="sha256-feature-hash-v1",
            dimension=self.dimension,
            available=True,
            detail="Degraded lexical hashing fallback; semantic model is not loaded.",
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        from .meili import tokenize

        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in tokenize(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                vector[index] += -1.0 if digest[4] & 1 else 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class SentenceTransformerEmbedder:
    """Lazy BGE-M3 loader with CUDA-to-CPU fallback."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        dimension: int = 1024,
        device: str | None = None,
        batch_size: int = 8,
        max_sequence_length: int = 1024,
        use_half_precision: bool = True,
    ) -> None:
        if max_sequence_length < 64:
            raise ValueError("max_sequence_length must be at least 64 tokens")
        self.model_name = model_name
        self.dimension = dimension
        self.requested_device = device
        self.batch_size = batch_size
        self.max_sequence_length = max_sequence_length
        self.use_half_precision = use_half_precision
        self._model: Any | None = None
        self._device: str | None = None
        self._precision = "float32"
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
                    model = sentence_transformers.SentenceTransformer(
                        self.model_name,
                        device=device,
                        trust_remote_code=False,
                    )
                except Exception:
                    if device == "cpu":
                        raise
                    device = "cpu"
                    model = sentence_transformers.SentenceTransformer(
                        self.model_name,
                        device=device,
                        trust_remote_code=False,
                    )
                model.max_seq_length = self.max_sequence_length
                precision = "float32"
                if device == "cuda" and self.use_half_precision:
                    try:
                        model = model.half()
                        precision = "float16"
                    except (AttributeError, RuntimeError):
                        precision = "float32"
            except Exception as exc:
                load_error = f"{type(exc).__name__}: {exc}"
                with self._state_lock:
                    self._load_error = load_error
                raise RuntimeError(f"Unable to load embedding model: {load_error}") from exc
            with self._state_lock:
                self._model = model
                self._device = device
                self._precision = precision
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
                self._precision = "float32"
                self._load_error = None
            if was_loaded:
                release_model_memory(device)
            return was_loaded

    @property
    def status(self) -> EmbeddingStatus:
        with self._operation_lock:
            try:
                self._load()
            except RuntimeError:
                with self._state_lock:
                    load_error = self._load_error
                return EmbeddingStatus(
                    provider="sentence-transformers",
                    model=self.model_name,
                    dimension=self.dimension,
                    available=False,
                    detail=load_error,
                )
            with self._state_lock:
                device = self._device
                precision = self._precision
            return EmbeddingStatus(
                provider="sentence-transformers",
                model=self.model_name,
                dimension=self.dimension,
                available=True,
                detail=(
                    f"device={device}; precision={precision}; "
                    f"max_sequence_length={self.max_sequence_length}"
                ),
            )

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._operation_lock:
            model = self._load()
            batch_size = max(1, self.batch_size)
            encoded: Any | None = None
            while True:
                try:
                    encoded = model.encode(
                        texts,
                        batch_size=batch_size,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                    )
                    break
                except RuntimeError:
                    if batch_size > 1:
                        batch_size = max(1, batch_size // 2)
                        continue
                    with self._state_lock:
                        device = self._device
                        precision = self._precision
                    if device == "cuda":
                        if precision == "float16":
                            model = model.float()
                            with self._state_lock:
                                self._precision = "float32"
                        model = model.to("cpu")
                        with self._state_lock:
                            self._model = model
                            self._device = "cpu"
                        continue
                    raise
            if encoded is None:
                raise RuntimeError("Embedding model returned no result")
            values = encoded.tolist()
            with self._state_lock:
                precision = self._precision
            if precision == "float16" and any(
                not math.isfinite(float(value)) for vector in values for value in vector
            ):
                # Rare pathological inputs can overflow FP16 normalization. Re-run
                # only that input group in FP32, then return the model to FP16 for
                # subsequent batches.
                model = model.float()
                try:
                    encoded = model.encode(
                        texts,
                        batch_size=max(1, batch_size // 2),
                        normalize_embeddings=True,
                        show_progress_bar=False,
                    )
                    values = encoded.tolist()
                finally:
                    model = model.half()
                    with self._state_lock:
                        self._model = model
            if any(not math.isfinite(float(value)) for vector in values for value in vector):
                raise ValueError("Embedding model returned a non-finite value")
            if any(len(vector) != self.dimension for vector in values):
                raise ValueError(
                    f"Embedding dimension mismatch; expected {self.dimension}, "
                    f"received {len(values[0]) if values else 0}"
                )
            return [[float(value) for value in vector] for vector in values]


class ResilientEmbedder:
    """Use BGE-M3 when available and expose an explicit degraded fallback."""

    def __init__(
        self,
        primary: Embedder | None = None,
        fallback: Embedder | None = None,
    ) -> None:
        self.primary = primary or SentenceTransformerEmbedder()
        self.fallback = fallback or HashingEmbedder()
        self._active: Embedder | None = None
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
            for embedder in (self.primary, self.fallback):
                if isinstance(embedder, ManagedModel):
                    released = embedder.unload() or released
            self._active = None
            self._error = None
            return released or was_active

    @property
    def status(self) -> EmbeddingStatus:
        with self._operation_lock:
            if self._active is self.fallback:
                fallback = self.fallback.status
                return EmbeddingStatus(
                    provider=fallback.provider,
                    model=fallback.model,
                    dimension=fallback.dimension,
                    available=True,
                    detail=f"degraded=true; primary_error={self._error}",
                )
            if self._active is self.primary:
                return self.primary.status
            status = self.primary.status
            if status.available:
                return status
            fallback = self.fallback.status
            return EmbeddingStatus(
                provider=fallback.provider,
                model=fallback.model,
                dimension=fallback.dimension,
                available=True,
                detail=f"degraded=true; primary_error={status.detail}",
            )

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self._operation_lock:
            if self._active is not None:
                return self._active.encode(texts)
            try:
                values = self.primary.encode(texts)
            except Exception as error:
                self._error = f"{type(error).__name__}: {error}"
                self._active = self.fallback
                return self.fallback.encode(texts)
            self._active = self.primary
            return values
