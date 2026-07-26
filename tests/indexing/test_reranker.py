from __future__ import annotations

import threading
from typing import Any

import pytest

from packages.indexing import ModelRuntimeStatus
from packages.indexing import rerank as rerank_module
from packages.indexing.models import SearchDocument, SearchHit
from packages.indexing.rerank import SentenceTransformerReranker


class _Scores(list[float]):
    def tolist(self) -> list[float]:
        return list(self)


class _RecordingCrossEncoder:
    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []

    def predict(self, pairs: list[tuple[str, str]], **_: Any) -> _Scores:
        self.pairs = pairs
        return _Scores([0.5 for _ in pairs])


class _BlockingCrossEncoder(_RecordingCrossEncoder):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def predict(self, pairs: list[tuple[str, str]], **kwargs: Any) -> _Scores:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test reranker inference did not resume")
        return super().predict(pairs, **kwargs)


def _hit() -> SearchHit:
    return SearchHit(
        document=SearchDocument(
            document_id="large",
            asset_id="asset",
            kind="code",
            title="Title",
            text="x" * 100_000,
        ),
        score=0.1,
        rank=1,
        source="hybrid",
    )


def test_reranker_bounds_text_before_cross_encoder_tokenization() -> None:
    model = _RecordingCrossEncoder()
    reranker = SentenceTransformerReranker(max_length=512)
    reranker._model = model
    reranker._device = "cpu"
    hit = _hit()

    result = reranker.rerank("query", [hit], limit=1)

    assert len(model.pairs[0][1]) == len("Title\n") + 32_768
    assert result[0].source.endswith("+bge-reranker")


def test_reranker_unload_waits_for_in_flight_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _BlockingCrossEncoder()
    reranker = SentenceTransformerReranker(max_length=512)
    reranker._model = model
    reranker._device = "cuda"
    released_devices: list[str | None] = []
    monkeypatch.setattr(
        rerank_module,
        "release_model_memory",
        lambda device: released_devices.append(device),
    )
    results: list[list[SearchHit]] = []
    errors: list[BaseException] = []

    def run_rerank() -> None:
        try:
            results.append(reranker.rerank("query", [_hit()], limit=1))
        except BaseException as error:
            errors.append(error)

    inference = threading.Thread(target=run_rerank)
    inference.start()
    assert model.started.wait(timeout=1)

    unload_started = threading.Event()
    unload_finished = threading.Event()

    def run_unload() -> None:
        unload_started.set()
        reranker.unload()
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
    assert len(results) == 1
    assert released_devices == ["cuda"]
    assert reranker.runtime_status == ModelRuntimeStatus(state="cold", device=None)
