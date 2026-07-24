"""Backend-neutral search contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BackendState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class BackendHealth:
    name: str
    state: BackendState
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SearchDocument:
    document_id: str
    asset_id: str
    kind: str
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    exact_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    document: SearchDocument
    score: float
    rank: int
    source: str
    exact_match: bool = False


@dataclass(frozen=True, slots=True)
class VectorPoint:
    point_id: str
    document: SearchDocument
    vector: tuple[float, ...]
