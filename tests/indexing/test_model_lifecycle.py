from __future__ import annotations

from typing import Any

import pytest

from packages.indexing import model_lifecycle


class _Cuda:
    def __init__(self) -> None:
        self.empty_cache_calls = 0

    @staticmethod
    def is_available() -> bool:
        return True

    def empty_cache(self) -> None:
        self.empty_cache_calls += 1


class _Torch:
    def __init__(self) -> None:
        self.cuda = _Cuda()


def test_release_model_memory_collects_gc_and_cuda_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gc_calls: list[None] = []
    torch = _Torch()

    monkeypatch.setattr(model_lifecycle, "_collect_garbage", lambda: gc_calls.append(None))

    def import_module(name: str) -> Any:
        assert name == "torch"
        return torch

    monkeypatch.setattr(model_lifecycle, "_import_module", import_module)

    model_lifecycle.release_model_memory("cuda:0")

    assert gc_calls == [None]
    assert torch.cuda.empty_cache_calls == 1
