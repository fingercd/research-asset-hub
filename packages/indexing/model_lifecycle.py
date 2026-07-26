"""Shared lifecycle primitives for lazily loaded local search models."""

from __future__ import annotations

import gc
import importlib
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

_collect_garbage = gc.collect
_import_module = importlib.import_module


@dataclass(frozen=True, slots=True)
class ModelRuntimeStatus:
    """A non-loading snapshot of a local model's process state."""

    state: Literal["cold", "warm"]
    device: str | None


@runtime_checkable
class ManagedModel(Protocol):
    """A model that can report and release its in-process resources."""

    @property
    def runtime_status(self) -> ModelRuntimeStatus: ...

    def unload(self) -> bool: ...


def release_model_memory(device: str | None) -> None:
    """Collect detached model objects and return unused CUDA memory to PyTorch."""

    _collect_garbage()
    if device is None or not device.casefold().startswith("cuda"):
        return
    try:
        torch: Any = _import_module("torch")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (AttributeError, ImportError, RuntimeError):
        # Unloading is best effort and must remain safe during interpreter shutdown.
        return
