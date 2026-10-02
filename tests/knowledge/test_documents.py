from uuid import uuid4

import pytest

from packages.knowledge.documents import (
    document_from_chunks,
    export_analysis_markdown,
    export_source_markdown,
)


def _document(source_type="original"):
    return document_from_chunks(
        asset_id="asset",
        representation_id="rep",
        source_hash="a" * 64,
        title="Paper",
        source_type=source_type,
        chunks=[
            {
                "id": str(uuid4()),
                "chunk_index": 1,
                "text_content": "second",
                "anchor": {"kind": "pdf", "page": 2, "source_path": "paper.pdf"},
            },
            {
                "id": "stable",
                "chunk_index": 0,
                "text_content": "first",
                "anchor": {
                    "kind": "pdf",
                    "page": 1,
                    "bbox": [1, 2, 3, 4],
                    "source_path": "paper.pdf",
                },
            },
        ],
    )


def test_page_reconstruction_and_stable_evidence():
    one, two = _document(), _document()
    assert [p["page"] for p in one["pages"]] == [1, 2]
    assert one["pages"][0]["evidence_id"] == two["pages"][0]["evidence_id"]
    assert one["pages"][0]["chunk_ids"] == ["chunk:stable"]
    assert one["pages"][0]["anchor"]["precision"] == "page"
    assert "bbox" not in one["pages"][0]["anchor"]


def test_exports_keep_provenance_and_analysis_separate():
    original = export_source_markdown(_document())
    analysis = export_analysis_markdown(_document("analysis"))
    assert "source_type: original" in original and "page: 2" in original
    assert "source_type: analysis" in analysis and "evidence_id:" in analysis
    with pytest.raises(ValueError):
        export_source_markdown(_document("analysis"))
    with pytest.raises(ValueError):
        export_analysis_markdown(_document())


def test_different_files_cannot_be_combined():
    with pytest.raises(ValueError, match="different source files"):
        document_from_chunks(
            asset_id="a",
            representation_id="r",
            source_hash="x",
            title="T",
            source_type="original",
            chunks=[
                {
                    "id": "1",
                    "chunk_index": 0,
                    "text_content": "a",
                    "anchor": {"source_path": "a.pdf"},
                },
                {
                    "id": "2",
                    "chunk_index": 1,
                    "text_content": "b",
                    "anchor": {"source_path": "b.pdf"},
                },
            ],
        )


def test_html_does_not_claim_pdf_page_precision():
    result = document_from_chunks(
        asset_id="a",
        representation_id="r",
        source_hash="x",
        title="T",
        source_type="analysis",
        chunks=[
            {
                "id": "1",
                "chunk_index": 0,
                "text_content": "text",
                "anchor": {"kind": "html", "selector": "#method"},
            },
        ],
    )
    assert result["pages"][0]["page"] is None
    assert result["pages"][0]["anchor"]["precision"] == "document"
