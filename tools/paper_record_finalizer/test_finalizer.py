from __future__ import annotations

import json
from pathlib import Path

from tools.paper_record_finalizer.finalize import (
    _abstract_from_pdf,
    _adavideorag_abstract_from_text,
    _adavideorag_unlabelled_abstract,
    _apply_emcompress,
    _apply_local_code_audit,
    _canonical_repository_url,
    _normal_title,
    _title_for_record,
)


def test_normal_title_removes_pdf_smallcaps_spacing() -> None:
    assert _normal_title("S PEC VLM: E FFICIENT V IDEO") == "SPEC VLM: EFFICIENT VIDEO"
    assert _normal_title("A Framework for V ISION") == "A Framework for VISION"
    assert _normal_title("S²Q-VDiT") == "S²Q-VDiT"


def test_longer_complete_prefix_wins() -> None:
    record = {
        "paper_id": "paper-2026",
        "title": "An Exact Complete Paper Title",
        "title_qa": {"record_key": "none"},
    }
    research = {"exact_title": "An Exact Complete"}
    assert _title_for_record(record, research, {}) == (
        "An Exact Complete Paper Title",
        "longer_complete_prefix_from_record",
    )


def test_known_primary_pdf_exception_beats_conflicting_agent_title() -> None:
    record = {
        "paper_id": "beyond-variance-2026",
        "title": "Beyond Variance: Activation Compression",
        "title_qa": {"record_key": "none"},
    }
    research = {"exact_title": "Beyond Variance: LLM Compression"}
    assert _title_for_record(record, research, {}) == (
        "Beyond Variance: Activation Compression",
        "primary_pdf_title",
    )


def test_unlabelled_abstract_is_not_invented(tmp_path: Path) -> None:
    # The helper reads only real PDF files; a non-PDF path must fail closed.
    path = tmp_path / "not-a-paper.pdf"
    path.write_text("no Abstract heading", encoding="utf-8")
    assert _abstract_from_pdf(str(path)) is None


def test_emcompress_current_pdf_is_recovered_28_page_version() -> None:
    record = {
        "paper_id": "emcompress-2026",
        "title": "Video-LLMs with Temporal Visual Screening",
        "pdf": {"path": "paper.pdf", "sha256": "legacy", "size": 26, "page_count": 26},
        "source_pdf_path": "legacy-paper.pdf",
        "target_pdf_path": "paper.pdf",
        "source_urls": [],
        "decision_evidence": [],
        "warnings": [],
    }
    recovery = {
        "paper_id": "emcompress-2026",
        "canonical_title": "EMCompress: Video-LLMs with Endomorphic Multimodal Compression",
        "alternate_title": "Video-LLMs with Temporal Visual Screening",
        "arxiv_id": "2508.21094",
        "local_path": "versions/arxiv-2508.21094.pdf",
        "sha256": "456caeac4afd8ed8565fd5930b82c45e28e16142650b8b2ebb8993dcdfffe19a",
        "bytes": 6020141,
        "pages": 28,
        "source_url": "https://arxiv.org/pdf/2508.21094",
        "evidence": [],
    }
    _apply_emcompress(record, recovery)
    assert record["pdf"]["sha256"] == recovery["sha256"]
    assert record["pdf"]["page_count"] == 28
    assert [item["role"] for item in record["pdf_representations"]] == [
        "canonical_current",
        "alternate_legacy_tvs",
    ]
    assert record["pdf_representations"][1]["sha256"] == "legacy"


def test_adavideorag_unlabelled_abstract_requires_exact_pdf_boundaries(tmp_path: Path) -> None:
    # The function only reads a PDF; the malformed fixture must fail closed.
    path = tmp_path / "not-a-paper.pdf"
    path.write_text("Multimodal Large Language Models (MLLMs)", encoding="utf-8")
    assert _adavideorag_unlabelled_abstract(str(path)) is None


def test_adavideorag_unlabelled_abstract_has_pdf_sentence_boundaries() -> None:
    text = (
        "AdaVideoRAG\nMultimodal Large Language Models (MLLMs) have demonstrated excellent "
        "performance in video understanding. establishing a new paradigm for adaptive retrieval "
        "augmentation in video analysis.\nDate: December 10, 2025"
    )
    value = _adavideorag_abstract_from_text(text)
    assert value is not None
    assert value.startswith("Multimodal Large Language Models")
    assert value.endswith("video analysis.")


def test_local_code_audit_upgrades_only_full_official_set(tmp_path: Path) -> None:
    core = tmp_path / "core"
    archive = core / "code" / "repo"
    archive.mkdir(parents=True)
    commit = "a" * 40
    url = "https://github.com/example/repository"
    records = []
    audit_rows = []
    for index in range(376):
        paper_id = f"paper-{index:03d}"
        public = index < 237
        records.append(
            {
                "paper_id": paper_id,
                "code": {
                    "research_status": "official_public" if public else "none_found",
                    "status": "official_url_only" if public else "none_found",
                    "url": url if public else None,
                    "commit": commit if public else None,
                    "evidence": [],
                },
                "finalization": {},
            }
        )
        selected = (
            {
                "ready": True,
                "url": url,
                "path": str(archive),
                "expected_commit": commit,
                "effective_license": "MIT",
                "evidence": {
                    "path": str(archive),
                    "inside_core": True,
                    "dirty": False,
                    "commit": commit,
                    "remotes": [{"name": "origin", "url": url}],
                },
            }
            if public
            else None
        )
        audit_rows.append(
            {
                "paper_id": paper_id,
                "status": "official_local" if public else "none_found",
                "selected_local_archive": selected,
            }
        )
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({"papers": audit_rows}), encoding="utf-8")
    assert _apply_local_code_audit(records, audit_path=audit_path, core_root=core) == 237
    assert all(record["code"]["status"] == "official_local" for record in records[:237])
    assert all(record["code"]["status"] == "none_found" for record in records[237:])


def test_canonical_repository_url_reduces_github_tree_link_to_repo() -> None:
    assert _canonical_repository_url("https://github.com/microsoft/MInference/tree/main/LeanK") == (
        "https://github.com/microsoft/minference"
    )
