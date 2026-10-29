"""Pure-Python extraction contracts and optional parser integrations."""

from .base import Extractor, NullOCRProvider, OCRProvider, PDFPageReader
from .code import CodeExtractor
from .html import HTMLExtractor
from .markdown import MarkdownExtractor
from .models import (
    Anchor,
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    HTMLAnchor,
    LineAnchor,
    NotebookAnchor,
    OCRProviderStatus,
    OCRState,
    PDFAnchor,
    TabularAnchor,
)
from .notebook import NotebookExtractor
from .ocr import TesseractOCRProvider
from .pdf import PDFExtractor, PyPDFPageReader
from .pipeline import ExtractionPipeline
from .tabular import TabularExtractor

__all__ = [
    "Anchor",
    "CodeExtractor",
    "ExtractionChunk",
    "ExtractionPipeline",
    "ExtractionResult",
    "ExtractionStatus",
    "Extractor",
    "HTMLAnchor",
    "HTMLExtractor",
    "LineAnchor",
    "MarkdownExtractor",
    "NotebookAnchor",
    "NotebookExtractor",
    "NullOCRProvider",
    "OCRProvider",
    "OCRProviderStatus",
    "OCRState",
    "PDFAnchor",
    "PDFExtractor",
    "PDFPageReader",
    "PyPDFPageReader",
    "TabularAnchor",
    "TabularExtractor",
    "TesseractOCRProvider",
]
