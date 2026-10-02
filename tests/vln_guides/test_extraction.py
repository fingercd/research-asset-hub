from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.scope_policy import DomainExclusionRule, DomainScopePolicy
from packages.vln_guides import extraction
from scripts.build_vln_unified_guide_inventory import DOMAIN, _asset_id_digest, _scope_audit

ASSET_ID = "11111111-1111-1111-1111-111111111111"


def _representation(representation_id: str, kind: str, path: str) -> dict[str, Any]:
    return {
        "id": representation_id,
        "type": kind,
        "source_alias": "source",
        "root_key": "documents",
        "path_ref": path,
        "local_state": "file",
        "sha256": "stale-database-sha",
    }


@pytest.fixture
def local_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"original PDF bytes")
    (source / "old.html").write_text("<p>First guide content</p>", encoding="utf-8")
    calls = {"pdf": 0}

    def run_pdf(pdf: Path, output: Path, executable: Path) -> str:
        calls["pdf"] += 1
        output.write_text("A complete page of text for paper extraction.\f", encoding="utf-8")
        return ""

    monkeypatch.setattr(extraction, "_run_pdftotext", run_pdf)
    monkeypatch.setattr(extraction, "_pdf_metadata", lambda _: {"page_count": 1})
    monkeypatch.setattr(extraction, "_extract_tables", lambda *_: [])
    return {
        "row": {
            "asset_id": ASSET_ID,
            "title": "Ground navigation",
            "primary_pdf": _representation("pdf-1", "pdf", "paper.pdf"),
            "existing_documents": [_representation("html-1", "html", "old.html")],
        },
        "registry": SourceRegistry({("source", "documents"): source}),
        "output_root": tmp_path / "extracted",
        "source": source,
        "calls": calls,
    }


def _extract(paper: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = extraction._extract_paper(
        paper["row"],
        registry=paper["registry"],
        output_root=paper["output_root"],
        pdftotext=Path("pdftotext"),
        force=False,
    )
    return result, json.loads(Path(result["report"]).read_text(encoding="utf-8"))


def test_visible_html_keeps_table_cells_and_caption_separate() -> None:
    text = extraction._visible_html_text(
        "<style>ignored</style><table><tr><th>Method</th><th>SR</th></tr>"
        "<tr><td>Ours</td><td>56.1</td></tr></table>"
        "<figcaption>Figure 1: overview.</figcaption><p>Next paragraph.</p>"
    )
    assert "Method SR" in text
    assert "Ours 56.1" in text
    assert "56.1\n" in text
    assert "Figure 1: overview.\n" in text
    assert "ignored" not in text


def test_cache_tracks_document_bytes_identity_type_path_and_membership(
    local_paper: dict[str, Any],
) -> None:
    first, report = _extract(local_paper)
    assert first["cache_state"] == "extracted"
    assert report["extractor"]["requested"] == "local-layout"
    assert report["extractor"]["used"] == "local-layout"
    assert _extract(local_paper)[0]["status"] == "cached"
    document = local_paper["row"]["existing_documents"][0]
    (local_paper["source"] / "old.html").write_text("<p>Updated guide</p>", encoding="utf-8")
    result, report = _extract(local_paper)
    assert result["cache_state"] == "documents_refreshed"
    assert report["existing_documents"][0]["text"] == "Updated guide"
    for key, value in [("id", "html-2"), ("type", "guide"), ("path_ref", "new.html")]:
        if key == "path_ref":
            (local_paper["source"] / value).write_text("<p>Updated guide</p>", encoding="utf-8")
        document[key] = value
        result, report = _extract(local_paper)
        assert result["cache_state"] == "documents_refreshed"
        assert (
            report["inputs"]["existing_documents"][0]["representation_id" if key == "id" else key]
            == value
        )
    local_paper["row"]["existing_documents"] = []
    result, report = _extract(local_paper)
    assert result["cache_state"] == "documents_refreshed"
    assert report["existing_documents"] == []
    assert local_paper["calls"]["pdf"] == 1


def test_pdf_reuse_follows_bytes_and_detects_modified_layout(local_paper: dict[str, Any]) -> None:
    _extract(local_paper)
    local_paper["row"]["primary_pdf"]["id"] = "pdf-replaced"
    assert _extract(local_paper)[0]["cache_state"] == "documents_refreshed"
    assert local_paper["calls"]["pdf"] == 1
    (local_paper["source"] / "paper.pdf").write_bytes(b"changed PDF bytes")
    assert _extract(local_paper)[0]["cache_state"] == "extracted"
    assert local_paper["calls"]["pdf"] == 2
    (local_paper["output_root"] / ASSET_ID / "layout.txt").write_text("broken", encoding="utf-8")
    assert _extract(local_paper)[0]["cache_state"] == "extracted"
    assert local_paper["calls"]["pdf"] == 3


def test_legacy_pdf_cache_refreshes_metadata_without_rerunning_pdf(
    local_paper: dict[str, Any],
) -> None:
    result, report = _extract(local_paper)
    report["extractor"] = {
        "version": extraction.PDF_EXTRACTOR_VERSION,
        "requested": "mineru",
        "used": "fallback",
    }
    report.pop("inputs")
    report["text"].pop("layout_sha256")
    report["recovery_notes"] = ["pdftotext page split count differs from pypdf page count: 2 != 1"]
    Path(result["report"]).write_text(json.dumps(report), encoding="utf-8")
    result, refreshed = _extract(local_paper)
    assert result["cache_state"] == "documents_refreshed"
    assert local_paper["calls"]["pdf"] == 1
    assert refreshed["extractor"]["requested"] == "local-layout"
    assert "recovered per-page output has 1 pages" in refreshed["recovery_notes"][0]


def test_missing_documents_and_cached_warnings_are_not_success(
    local_paper: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (local_paper["source"] / "old.html").unlink()
    result, report = _extract(local_paper)
    assert result["extraction_status"] == "warning"
    assert "existing document unavailable: html-1:missing" in report["warnings"]
    monkeypatch.setattr(extraction.shutil, "which", lambda _: "pdftotext")
    batch = extraction.extract_inventory(
        {"items": [local_paper["row"]]},
        registry=local_paper["registry"],
        output_root=local_paper["output_root"],
        workers=1,
    )
    assert batch["status"] == "warning"
    assert batch["items"][0]["status"] == "cached"
    assert batch["items"][0]["extraction_status"] == "warning"


def test_recovered_pagination_is_recorded_without_discarding_font_warning(
    local_paper: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extraction, "_pdf_metadata", lambda _: {"page_count": 2})
    monkeypatch.setattr(
        extraction,
        "_run_pdftotext_per_page",
        lambda *_, **__: (
            ["First page with enough meaningful content.", "Second page with enough content."],
            ["Font warning remains relevant"],
        ),
    )
    result, report = _extract(local_paper)
    assert report["text"]["page_splits"] == 2
    assert len(report["recovery_notes"]) == 1
    assert "1 initial splits -> 2 pages" in report["recovery_notes"][0]
    assert report["warnings"] == ["Font warning remains relevant"]
    assert result["extraction_status"] == "warning"


def test_scope_audit_checks_ids_and_titles_and_preserves_cross_embodiment() -> None:
    policy = DomainScopePolicy(
        [
            DomainExclusionRule(
                rule_id="exclude-uav",
                domain=DOMAIN,
                reason="User excludes drones",
                asset_ids=frozenset([ASSET_ID]),
                title_patterns=(re.compile(r"\bUAV\b"),),
            )
        ]
    )
    rows = [
        {"asset_id": ASSET_ID, "title": "Historical excluded title"},
        {"asset_id": "22222222-2222-2222-2222-222222222222", "title": "A UAV paper"},
        {"asset_id": "33333333-3333-3333-3333-333333333333", "title": "Embodied4C"},
        {
            "asset_id": "44444444-4444-4444-4444-444444444444",
            "title": "Embodied Navigation Foundation Model",
        },
    ]
    report = _scope_audit(rows, policy)
    assert report["status"] == "failed"
    assert report["match_count"] == 2
    assert report["matches"][0]["asset_id_match"] is True
    assert report["matches"][1]["title_match"] is True
    assert _scope_audit(rows[2:], policy)["status"] == "passed"
    assert _asset_id_digest(rows) == _asset_id_digest(list(reversed(rows)))
    assert _asset_id_digest(rows) != _asset_id_digest(rows[1:])
