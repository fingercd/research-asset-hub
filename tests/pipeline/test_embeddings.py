from __future__ import annotations

import math

import pytest

from packages.indexing import HashingEmbedder, SentenceTransformerEmbedder


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbedder(dimension=32)
    first, second = embedder.encode(["世界 模型", "世界 模型"])
    assert first == second
    assert len(first) == 32
    assert math.isclose(sum(value * value for value in first), 1.0)


def test_sentence_transformer_rejects_unsafe_sequence_floor() -> None:
    with pytest.raises(ValueError, match="at least 64"):
        SentenceTransformerEmbedder(max_sequence_length=32)
