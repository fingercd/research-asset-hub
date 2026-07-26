from __future__ import annotations

import math
import threading
from typing import Any

import pytest

from packages.indexing import (
    EmbeddingStatus,
    HashingEmbedder,
    ModelRuntimeStatus,
    ResilientEmbedder,
    SentenceTransformerEmbedder,
)
from packages.indexing import embeddings as embeddings_module


class _VectorBatch(list[list[float]]):
    def tolist(self) -> list[list[float]]:
        return list(self)


class _BlockingEmbeddingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, *_: Any, **__: Any) -> _VectorBatch:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test embedding inference did not resume")
        return _VectorBatch([[1.0, 0.0]])


class _RecoveringBgeEmbedder:
    def __init__(self) -> None:
        self.calls = 0
        self.loaded = False

    @property
    def status(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            provider="sentence-transformers",
            model="BAAI/bge-m3",
            dimension=2,
            available=True,
            detail="device=cpu",
        )

    @property
    def runtime_status(self) -> ModelRuntimeStatus:
        return ModelRuntimeStatus(
            state="warm" if self.loaded else "cold",
            device="cpu" if self.loaded else None,
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary load failure")
        self.loaded = True
        return [[1.0, 0.0] for _ in texts]

    def unload(self) -> bool:
        was_loaded = self.loaded
        self.loaded = False
        return was_loaded


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbedder(dimension=32)
    first, second = embedder.encode(["世界 模型", "世界 模型"])
    assert first == second
    assert len(first) == 32
    assert math.isclose(sum(value * value for value in first), 1.0)


def test_sentence_transformer_rejects_unsafe_sequence_floor() -> None:
    with pytest.raises(ValueError, match="at least 64"):
        SentenceTransformerEmbedder(max_sequence_length=32)


def test_embedding_unload_waits_for_in_flight_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _BlockingEmbeddingModel()
    embedder = SentenceTransformerEmbedder(dimension=2)
    embedder._model = model
    embedder._device = "cpu"
    released_devices: list[str | None] = []
    monkeypatch.setattr(
        embeddings_module,
        "release_model_memory",
        lambda device: released_devices.append(device),
    )
    encoded: list[list[list[float]]] = []
    errors: list[BaseException] = []

    def run_encode() -> None:
        try:
            encoded.append(embedder.encode(["query"]))
        except BaseException as error:
            errors.append(error)

    inference = threading.Thread(target=run_encode)
    inference.start()
    assert model.started.wait(timeout=1)

    unload_started = threading.Event()
    unload_finished = threading.Event()

    def run_unload() -> None:
        unload_started.set()
        embedder.unload()
        unload_finished.set()

    unloading = threading.Thread(target=run_unload)
    unloading.start()
    assert unload_started.wait(timeout=1)
    assert not unload_finished.wait(timeout=0.05)

    model.release.set()
    inference.join(timeout=1)
    unloading.join(timeout=1)

    assert not inference.is_alive()
    assert not unloading.is_alive()
    assert errors == []
    assert encoded == [[[1.0, 0.0]]]
    assert released_devices == ["cpu"]
    assert embedder.runtime_status == ModelRuntimeStatus(state="cold", device=None)


def test_resilient_embedder_retries_bge_after_idle_unload() -> None:
    primary = _RecoveringBgeEmbedder()
    embedder = ResilientEmbedder(primary, HashingEmbedder(dimension=2))

    assert len(embedder.encode(["first"])[0]) == 2
    assert len(embedder.encode(["still degraded"])[0]) == 2
    assert primary.calls == 1

    assert embedder.unload()
    assert embedder.encode(["retry"]) == [[1.0, 0.0]]
    assert primary.calls == 2
    assert embedder.status.model == "BAAI/bge-m3"
