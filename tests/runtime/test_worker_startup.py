from unittest.mock import MagicMock, patch

from apps.api.config import ApiSettings
from apps.worker.handlers import RuntimeHandlers


def test_worker_defers_extractor_and_ocr_discovery_until_first_use() -> None:
    with patch("packages.extraction.ExtractionPipeline") as build:
        handlers = RuntimeHandlers(MagicMock(), ApiSettings(), MagicMock())
        build.assert_not_called()
        first = handlers.extraction_pipeline
        assert handlers.extraction_pipeline is first
        build.assert_called_once_with(enable_tree_sitter=False)


def test_worker_keeps_an_explicit_extraction_pipeline() -> None:
    supplied = MagicMock()
    with patch("packages.extraction.ExtractionPipeline") as build:
        handlers = RuntimeHandlers(
            MagicMock(), ApiSettings(), MagicMock(), extraction_pipeline=supplied
        )
        assert handlers.extraction_pipeline is supplied
        build.assert_not_called()
