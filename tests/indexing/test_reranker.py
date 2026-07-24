from __future__ import annotations

from typing import Any

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


def test_reranker_bounds_text_before_cross_encoder_tokenization() -> None:
    model = _RecordingCrossEncoder()
    reranker = SentenceTransformerReranker(max_length=512)
    reranker._model = model
    reranker._device = "cpu"
    hit = SearchHit(
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

    result = reranker.rerank("query", [hit], limit=1)

    assert len(model.pairs[0][1]) == len("Title\n") + 32_768
    assert result[0].source.endswith("+bge-reranker")
