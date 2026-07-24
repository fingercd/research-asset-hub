from __future__ import annotations

import json
from pathlib import Path

from packages.extraction import (
    CodeExtractor,
    ExtractionPipeline,
    ExtractionStatus,
    HTMLExtractor,
    MarkdownExtractor,
    NotebookExtractor,
    OCRProviderStatus,
    OCRState,
    PDFExtractor,
    TabularExtractor,
)


class FakePageReader:
    @property
    def name(self) -> str:
        return "fake-pages"

    def read_pages(self, path: Path) -> list[str]:
        return ["x", "Enough extracted text for the second page."]


class FakeOCR:
    @property
    def status(self) -> OCRProviderStatus:
        return OCRProviderStatus("fake-ocr", True, True)

    def recognize_page(self, path: Path, page_number: int) -> str:
        return f"OCR text for page {page_number}"


def test_pdf_uses_page_anchors_and_ocr_status(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-test")
    result = PDFExtractor(
        page_reader=FakePageReader(),
        ocr_provider=FakeOCR(),
        low_text_density=10,
    ).extract(path, representation_id="pdf-1")

    assert result.status == ExtractionStatus.SUCCEEDED
    assert result.ocr_state == OCRState.COMPLETED
    assert all(chunk.anchor["kind"] == "pdf" for chunk in result.chunks)
    pages = [
        chunk.anchor["page"]
        for chunk in result.chunks
        if chunk.anchor["kind"] == "pdf"
    ]
    assert pages == [1, 2]
    assert result.chunks[0].text == "OCR text for page 1"
    assert all(len(chunk.content_hash) == 64 for chunk in result.chunks)


def test_chunks_replace_database_unsafe_nul_bytes(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.py"
    path.write_bytes(b"value = 'safe'\x00\n")
    result = CodeExtractor().extract(path, representation_id="code-nul")
    assert result.chunks
    assert all("\x00" not in chunk.text for chunk in result.chunks)
    assert any("\ufffd" in chunk.text for chunk in result.chunks)


def test_tree_sitter_extracts_typescript_symbols(tmp_path: Path) -> None:
    path = tmp_path / "trainer.ts"
    path.write_text(
        "export function trainStep(value: number): number {\n"
        "  return value + 1;\n"
        "}\n",
        encoding="utf-8",
    )
    result = CodeExtractor().extract(path, representation_id="code-typescript")
    assert result.metadata["parser"] == "tree_sitter"
    assert any(chunk.anchor.get("symbol") == "trainStep" for chunk in result.chunks)


def test_native_risk_languages_use_bounded_plain_text_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kernel.cpp"
    path.write_text(
        "int main() {\n  return 0;\n}\n",
        encoding="utf-8",
    )
    result = CodeExtractor().extract(path, representation_id="cpp-fallback")
    assert result.metadata["parser"] == "plain_text"
    assert result.chunks[0].anchor["kind"] == "line"


def test_tree_sitter_can_be_disabled_for_crash_isolation(tmp_path: Path) -> None:
    path = tmp_path / "worker.ts"
    path.write_text(
        "export function work(): number { return 1; }\n",
        encoding="utf-8",
    )
    result = CodeExtractor(enable_tree_sitter=False).extract(
        path,
        representation_id="safe-worker",
    )
    assert result.metadata["parser"] == "plain_text"
    assert result.metadata["tree_sitter_enabled"] is False


def test_code_extraction_caps_symbols_and_chunk_text(tmp_path: Path) -> None:
    path = tmp_path / "large.py"
    path.write_text(
        "\n\n".join(
            f"def symbol_{index}():\n    return {'x' * 120!r}"
            for index in range(5)
        ),
        encoding="utf-8",
    )
    result = CodeExtractor(
        max_symbols_per_file=2,
        max_chunk_characters=80,
    ).extract(path, representation_id="bounded-code")

    assert len(result.chunks) == 2
    assert all(len(chunk.text) <= 82 for chunk in result.chunks)
    assert all(chunk.metadata["truncated"] is True for chunk in result.chunks)


def test_html_prefers_main_and_preserves_heading_anchor(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text(
        """
        <html><body>
          <nav>navigation noise</nav>
          <main><h1 id="method">Method</h1><p>Useful content.</p></main>
          <script>secretNoise()</script>
        </body></html>
        """,
        encoding="utf-8",
    )
    result = HTMLExtractor().extract(path, representation_id="html-1")
    assert result.status == ExtractionStatus.SUCCEEDED
    assert len(result.chunks) == 1
    assert result.chunks[0].text == "Useful content."
    assert result.chunks[0].anchor["kind"] == "html"
    assert result.chunks[0].anchor["dom_id"] == "method"


def test_markdown_and_code_keep_line_and_symbol_anchors(tmp_path: Path) -> None:
    markdown = tmp_path / "note.md"
    markdown.write_text("# Intro\nfirst\n## Detail\nsecond\n", encoding="utf-8")
    markdown_result = MarkdownExtractor().extract(
        markdown,
        representation_id="md-1",
    )
    assert [chunk.title for chunk in markdown_result.chunks] == ["Intro", "Detail"]
    markdown_anchor = markdown_result.chunks[1].anchor
    assert markdown_anchor["kind"] == "line"
    assert markdown_anchor["line_start"] == 3

    code = tmp_path / "sample.py"
    code.write_text(
        "def train(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    code_result = CodeExtractor().extract(code, representation_id="code-1")
    assert code_result.metadata["parser"] == "python_ast"
    code_anchor = code_result.chunks[0].anchor
    assert code_anchor["kind"] == "line"
    assert code_anchor["symbol"] == "train"
    assert code_anchor["line_start"] == 1


def test_notebook_skips_binary_output_but_indexes_text(tmp_path: Path) -> None:
    path = tmp_path / "study.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# Note"]},
                    {
                        "cell_type": "code",
                        "source": ["print('ok')"],
                        "outputs": [
                            {
                                "data": {
                                    "text/plain": ["ok"],
                                    "image/png": "base64",
                                }
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = NotebookExtractor().extract(path, representation_id="nb-1")
    assert result.status == ExtractionStatus.PARTIAL
    assert len(result.chunks) == 3
    assert any("binary image" in warning for warning in result.warnings)
    output_anchor = result.chunks[-1].anchor
    assert output_anchor["kind"] == "notebook"
    assert output_anchor["output_index"] == 0


def test_large_tabular_input_indexes_schema_and_sample(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text(
        "\n".join(json.dumps({"id": index, "value": f"v{index}"}) for index in range(5)),
        encoding="utf-8",
    )
    result = TabularExtractor(
        full_index_bytes=1,
        sample_rows=2,
    ).extract(path, representation_id="table-1")
    assert result.status == ExtractionStatus.PARTIAL
    assert result.metadata["row_count"] == 5
    assert result.metadata["columns"] == ["id", "value"]
    table_anchor = result.chunks[0].anchor
    assert table_anchor["kind"] == "tabular"
    assert table_anchor["row_end"] == 2


def test_pipeline_reuses_only_same_hash_and_extractor_version(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("first", encoding="utf-8")
    pipeline = ExtractionPipeline([MarkdownExtractor()])
    first = pipeline.extract(path, representation_id="text-1")
    second = pipeline.extract(path, representation_id="text-1")
    assert second is first

    path.write_text("second", encoding="utf-8")
    third = pipeline.extract(path, representation_id="text-1")
    assert third.representation_hash != first.representation_hash
    assert pipeline.invalidate("text-1") == 2


def test_pipeline_cache_has_a_hard_entry_bound(tmp_path: Path) -> None:
    first_path = tmp_path / "first.md"
    second_path = tmp_path / "second.md"
    first_path.write_text("# First", encoding="utf-8")
    second_path.write_text("# Second", encoding="utf-8")
    pipeline = ExtractionPipeline([MarkdownExtractor()], cache_entries=1)

    pipeline.extract(first_path, representation_id="first")
    pipeline.extract(second_path, representation_id="second")

    assert pipeline.invalidate("first") == 0
    assert pipeline.invalidate("second") == 1
