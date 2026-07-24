"""Protocols and common helpers for extraction backends."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .models import ExtractionResult, OCRProviderStatus


class Extractor(Protocol):
    name: str
    version: str

    def supports(self, path: Path) -> bool: ...

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult: ...


class OCRProvider(Protocol):
    @property
    def status(self) -> OCRProviderStatus: ...

    def recognize_page(self, path: Path, page_number: int) -> str: ...


class PDFPageReader(Protocol):
    @property
    def name(self) -> str: ...

    def read_pages(self, path: Path) -> Iterable[str]: ...


class NullOCRProvider:
    @property
    def status(self) -> OCRProviderStatus:
        return OCRProviderStatus(
            provider="none",
            available=False,
            configured=False,
            detail="No OCR provider configured",
        )

    def recognize_page(self, path: Path, page_number: int) -> str:
        raise RuntimeError("OCR provider is unavailable")
