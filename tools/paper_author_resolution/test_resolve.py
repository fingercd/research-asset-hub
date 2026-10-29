from __future__ import annotations

from tools.paper_author_resolution.resolve import (
    _clean_display,
    _split_clear_comma_list,
    resolve_rows,
)


def _rows(raw: str, key: str = "desktop-a" * 1) -> tuple[list[dict], list[dict], list[dict]]:
    digest = "a" * 64
    qa = [
        {
            "record_key": key,
            "source": {"sha256": digest},
            "authors": {
                "accepted": None,
                "decision": "review_low_confidence",
                "candidates": [{"value": raw, "confidence": 0.68, "evidence": {"lines": []}}],
            },
        }
    ]
    records = [{"paper_id": "paper-a", "title": "A Paper", "pdf": {"sha256": digest}}]
    metadata = [{"source": {"sha256": digest}}]
    return qa, records, metadata


def test_clean_display_removes_markers_and_affiliation_tail() -> None:
    assert (
        _clean_display("1,2 ∗ Ada Lovelace, Alan Turing† University of Example")
        == "Ada Lovelace, Alan Turing"
    )


def test_clear_comma_list_is_split_but_flattened_text_is_not() -> None:
    assert _split_clear_comma_list("Ada Lovelace, Alan Turing") == ["Ada Lovelace", "Alan Turing"]
    assert _split_clear_comma_list("Ada Lovelace Alan Turing") is None


def test_resolve_preserves_flattened_layout_as_display() -> None:
    qa, records, metadata = _rows("Ada Lovelace Alan Turing")
    rows, counts = resolve_rows(qa, records, metadata, expected_count=1)
    assert rows[0]["authors_display"] == "Ada Lovelace Alan Turing"
    assert rows[0]["status"] == "verified_from_pdf_first_page_layout"
    assert counts["authors_display"] == 1
