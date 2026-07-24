"""Extractor routing and content/version-aware incremental reuse."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from .base import Extractor
from .code import CodeExtractor
from .html import HTMLExtractor
from .markdown import MarkdownExtractor
from .models import ExtractionResult, ExtractionStatus, hash_bytes
from .notebook import NotebookExtractor
from .ocr import TesseractOCRProvider
from .pdf import PDFExtractor
from .tabular import TabularExtractor


class ExtractionPipeline:
    def __init__(
        self,
        extractors: list[Extractor] | None = None,
        *,
        cache_entries: int = 32,
        enable_tree_sitter: bool = True,
    ) -> None:
        if cache_entries < 0:
            raise ValueError("cache_entries cannot be negative")
        self.extractors = extractors or [
            PDFExtractor(ocr_provider=TesseractOCRProvider()),
            HTMLExtractor(),
            NotebookExtractor(),
            TabularExtractor(),
            CodeExtractor(enable_tree_sitter=enable_tree_sitter),
            MarkdownExtractor(),
        ]
        self.cache_entries = cache_entries
        self._cache: OrderedDict[
            tuple[str, str, str, str],
            ExtractionResult,
        ] = OrderedDict()

    def select(self, path: Path) -> Extractor | None:
        return next(
            (extractor for extractor in self.extractors if extractor.supports(path)),
            None,
        )

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        extractor = self.select(path)
        representation_hash = hash_bytes(path.read_bytes())
        if extractor is None:
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=representation_hash,
                extractor="none",
                extractor_version="0",
                status=ExtractionStatus.UNSUPPORTED,
                warnings=(f"No extractor registered for {path.suffix}",),
            )
        key = (
            representation_id,
            representation_hash,
            extractor.name,
            extractor.version,
        )
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        result = extractor.extract(path, representation_id=representation_id)
        if self.cache_entries:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_entries:
                self._cache.popitem(last=False)
        return result

    def invalidate(self, representation_id: str) -> int:
        keys = [key for key in self._cache if key[0] == representation_id]
        for key in keys:
            del self._cache[key]
        return len(keys)
