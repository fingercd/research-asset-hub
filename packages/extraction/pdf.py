"""Page-oriented PDF extraction with injectable OCR and parser providers."""

from __future__ import annotations

import importlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .base import NullOCRProvider, OCRProvider, PDFPageReader
from .models import (
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    OCRState,
    PDFAnchor,
    hash_bytes,
    make_chunk,
)


class PyPDFPageReader:
    @property
    def name(self) -> str:
        return "pypdf"

    def read_pages(self, path: Path) -> list[str]:
        module: Any = importlib.import_module("pypdf")
        reader = module.PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages]


class PDFPlumberPageReader:
    """Extract page text and page-coordinate bounds with pdfplumber."""

    def __init__(self) -> None:
        self.page_boxes: list[tuple[float, float, float, float]] = []

    @property
    def name(self) -> str:
        return "pdfplumber"

    def read_pages(self, path: Path) -> list[str]:
        module: Any = importlib.import_module("pdfplumber")
        pages: list[str] = []
        boxes: list[tuple[float, float, float, float]] = []
        with module.open(str(path)) as document:
            for page in document.pages:
                pages.append(page.extract_text() or "")
                boxes.append((0.0, 0.0, float(page.width), float(page.height)))
        self.page_boxes = boxes
        return pages


class PDFExtractor:
    name = "pdf"
    version = "2"

    def __init__(
        self,
        *,
        page_reader: PDFPageReader | None = None,
        ocr_provider: OCRProvider | None = None,
        low_text_density: int = 80,
    ) -> None:
        self.page_reader = page_reader or PDFPlumberPageReader()
        self.ocr_provider = ocr_provider or NullOCRProvider()
        self.low_text_density = low_text_density

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() == ".pdf"

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        raw = path.read_bytes()
        representation_hash = hash_bytes(raw)
        try:
            pages = list(self.page_reader.read_pages(path))
        except (ImportError, ModuleNotFoundError) as exc:
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=representation_hash,
                extractor=self.name,
                extractor_version=self.version,
                status=ExtractionStatus.UNSUPPORTED,
                warnings=(f"PDF parser unavailable: {exc}",),
                metadata={"page_reader": self.page_reader.name},
            )
        except Exception as exc:
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=representation_hash,
                extractor=self.name,
                extractor_version=self.version,
                status=ExtractionStatus.FAILED,
                warnings=(f"PDF parsing failed: {exc}",),
                metadata={"page_reader": self.page_reader.name},
            )

        chunks: list[ExtractionChunk] = []
        warnings: list[str] = []
        ocr_state = OCRState.NOT_NEEDED
        page_characters: list[int] = []
        page_boxes = list(getattr(self.page_reader, "page_boxes", []))
        for page_number, extracted in enumerate(pages, start=1):
            text = extracted.strip()
            page_characters.append(len(text))
            if len(text) < self.low_text_density:
                provider_status = self.ocr_provider.status
                if provider_status.available and provider_status.configured:
                    try:
                        ocr_text = self.ocr_provider.recognize_page(path, page_number).strip()
                    except Exception as exc:
                        warnings.append(f"OCR failed on page {page_number}: {exc}")
                        ocr_state = OCRState.FAILED
                    else:
                        if ocr_text:
                            text = ocr_text
                            ocr_state = OCRState.COMPLETED
                else:
                    warnings.append(f"Page {page_number} requires OCR")
                    ocr_state = OCRState.UNAVAILABLE
            if not text:
                continue
            anchor: PDFAnchor = {"kind": "pdf", "page": page_number}
            if page_number <= len(page_boxes):
                anchor["bbox"] = page_boxes[page_number - 1]
            chunks.append(
                make_chunk(
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                    ordinal=len(chunks),
                    text=text,
                    extractor=self.name,
                    extractor_version=self.version,
                    anchor=anchor,
                    title=f"Page {page_number}",
                    metadata={"page": page_number, "characters": len(text)},
                )
            )
        status = ExtractionStatus.SUCCEEDED
        if not chunks:
            status = ExtractionStatus.FAILED
        elif warnings:
            status = ExtractionStatus.PARTIAL
        return ExtractionResult(
            representation_id=representation_id,
            source_path=str(path),
            representation_hash=representation_hash,
            extractor=self.name,
            extractor_version=self.version,
            status=status,
            chunks=tuple(chunks),
            warnings=tuple(warnings),
            ocr_state=ocr_state,
            metadata={
                "page_count": len(pages),
                "page_characters": page_characters,
                "page_reader": self.page_reader.name,
                "ocr_provider": asdict(self.ocr_provider.status),
            },
        )
