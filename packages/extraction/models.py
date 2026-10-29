"""Stable extraction contracts shared by parsers and indexers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, NotRequired, TypeAlias, TypedDict


class ExtractionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class OCRState(StrEnum):
    NOT_NEEDED = "not_needed"
    PENDING = "pending"
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class PDFAnchor(TypedDict):
    kind: Literal["pdf"]
    page: int
    bbox: NotRequired[tuple[float, float, float, float]]


class HTMLAnchor(TypedDict):
    kind: Literal["html"]
    heading: str
    dom_id: NotRequired[str]
    selector: NotRequired[str]


class LineAnchor(TypedDict):
    kind: Literal["line"]
    path: str
    line_start: int
    line_end: int
    symbol: NotRequired[str]


class NotebookAnchor(TypedDict):
    kind: Literal["notebook"]
    cell_index: int
    cell_type: str
    output_index: NotRequired[int]


class TabularAnchor(TypedDict):
    kind: Literal["tabular"]
    row_start: int
    row_end: int
    columns: NotRequired[list[str]]


Anchor: TypeAlias = PDFAnchor | HTMLAnchor | LineAnchor | NotebookAnchor | TabularAnchor


@dataclass(frozen=True, slots=True)
class OCRProviderStatus:
    provider: str
    available: bool
    configured: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionChunk:
    chunk_id: str
    representation_id: str
    ordinal: int
    text: str
    content_hash: str
    extractor: str
    extractor_version: str
    anchor: Anchor
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    representation_id: str
    source_path: str
    representation_hash: str
    extractor: str
    extractor_version: str
    status: ExtractionStatus
    chunks: tuple[ExtractionChunk, ...] = ()
    warnings: tuple[str, ...] = ()
    ocr_state: OCRState = OCRState.NOT_NEEDED
    metadata: dict[str, Any] = field(default_factory=dict)


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_text(value: str) -> str:
    return hash_bytes(value.encode("utf-8"))


def make_chunk(
    *,
    representation_id: str,
    representation_hash: str,
    ordinal: int,
    text: str,
    extractor: str,
    extractor_version: str,
    anchor: Anchor,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExtractionChunk:
    normalized_text = text.replace("\x00", "\ufffd")
    content_hash = hash_text(normalized_text)
    identity = json.dumps(
        {
            "representation_id": representation_id,
            "representation_hash": representation_hash,
            "ordinal": ordinal,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "anchor": anchor,
            "content_hash": content_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ExtractionChunk(
        chunk_id=hash_text(identity),
        representation_id=representation_id,
        ordinal=ordinal,
        text=normalized_text,
        content_hash=content_hash,
        extractor=extractor,
        extractor_version=extractor_version,
        anchor=anchor,
        title=title,
        metadata=dict(metadata or {}),
    )
