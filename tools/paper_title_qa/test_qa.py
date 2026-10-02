from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.paper_title_qa import qa
from tools.paper_title_qa.prepare import prepare_inputs
from tools.paper_title_qa.qa import TextLine


def _line(text: str, size: float, x: float, y: float, x1: float = 500) -> TextLine:
    return TextLine(text=text, font_size=size, x0=x, y=y, x1=x1)


def _body_lines() -> list[TextLine]:
    return [
        _line("Abstract", 12, 158, 611, 220),
        _line("This paper studies a representative body-text sentence.", 10, 88, 586, 290),
        _line("A second body-text sentence makes the modal font stable.", 10, 88, 574, 290),
        _line("A third body-text sentence makes the modal font stable.", 10, 88, 562, 290),
    ]


def test_a11y_layout_title_and_authors() -> None:
    lines = [
        _line("A11y-Compressor: A Framework for", 14.3, 183, 752, 410),
        _line(
            "Enhancing the Efficiency of GUI Agent Observations through",
            14.3,
            109,
            736,
            490,
        ),
        _line("Visual Context Reconstruction and Redundancy Reduction", 14.3, 116, 720, 480),
        _line("Michito Takeshita Takuro Kawada Takumi Ohashi", 12, 157, 691, 440),
        _line("Shunsuke Kitada Hitoshi Iyatomi", 12, 206, 677, 390),
        _line("Hosei University, Tokyo, Japan", 12, 223, 663, 390),
        *_body_lines(),
    ]
    titles, rejected = qa._title_blocks(lines, 595.276, 841.89)
    assert rejected == []
    assert titles[0]["value"] == (
        "A11y-Compressor: A Framework for Enhancing the Efficiency of GUI Agent "
        "Observations through Visual Context Reconstruction and Redundancy Reduction"
    )
    authors = qa._author_layout_candidate(lines, titles[0], 841.89)
    assert authors is not None
    assert "Michito Takeshita" in authors["value"]
    assert "Hosei University" not in authors["value"]


def test_recovered_video_rejects_cvpr_proceedings_header() -> None:
    lines = [
        _line(
            "This CVPR paper is the Open Access version, provided by the Computer Vision "
            "Foundation.",
            9.3,
            132,
            781,
        ),
        _line("Except for this watermark, it is identical to the accepted version;", 9.3, 185, 770),
        _line(
            "the final published version of the proceedings is available on IEEE Xplore.",
            9.3,
            165,
            758,
        ),
        _line("FlexiVideo: Variation-Aware Temporal Dynamics Modeling for", 14.3, 108, 675),
        _line("Efficient Video Understanding", 14.3, 213, 657, 420),
        _line("Da Peng Xuesong Yang Zonghao Guo Yichen Zhang Chi Chen", 12, 125, 623),
        *_body_lines(),
    ]
    titles, rejected = qa._title_blocks(lines, 612, 792)
    assert titles[0]["value"] == (
        "FlexiVideo: Variation-Aware Temporal Dynamics Modeling for Efficient Video Understanding"
    )
    assert any(item["reason"] == "boilerplate_header" for item in rejected)
    assert all("Open Access version" not in item["value"] for item in titles)
    assert all("proceedings" not in item["value"].casefold() for item in titles)


def test_vln_cross_evidence_accepts_title() -> None:
    title = (
        "Walk and Read Less: Improving the Efficiency of Vision-and-Language Navigation "
        "via Tuning-Free Multimodal Token Pruning"
    )
    candidates = [
        qa._candidate(title, "pypdf_layout", 0.78),
        qa._candidate(title, "mineru_h1", 0.72),
        qa._candidate(title[:-20], "filename", 0.48, {"may_be_truncated": True}),
    ]
    ranked = qa._rank_title_candidates(
        candidates,
        {"available": True, "lines": [title], "error": None},
        ["Navigation-Aware Pruning (NAP)"],
        0.78,
        0.06,
    )
    assert ranked["accepted"] == title
    assert ranked["decision"] == "accepted_high_confidence"
    assert ranked["candidates"][0]["confidence"] >= 0.9


def test_wrong_video_filename_is_visible_conflict() -> None:
    actual = "Video-LLMs with Temporal Visual Screening"
    filename = "EMCompress: Video-LLMs with Endomorphic Multimodal Compression"
    ranked = qa._rank_title_candidates(
        [
            qa._candidate(actual, "pypdf_layout", 0.78),
            qa._candidate(actual, "mineru_h1", 0.72),
            qa._candidate(filename, "filename", 0.48),
        ],
        {"available": True, "lines": [actual], "error": None},
        ["Temporal Visual Screening (TVS)"],
        0.78,
        0.06,
    )
    conflicts = qa._conflicts(ranked, {"accepted": None, "candidates": []}, [])
    assert ranked["accepted"] == actual
    assert any(
        item["kind"] == "strong_source_disagreement" and item["other_source"] == "filename"
        for item in conflicts
    )


def test_batch_checkpoint_uses_sha_and_keeps_per_file_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-dummy-A")
    missing = tmp_path / "missing.pdf"
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "good", "pdf_path": str(pdf)}),
                json.dumps({"id": "missing", "pdf_path": str(missing)}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "result.jsonl"
    calls: list[str] = []

    def fake_analyze(item: dict, **_kwargs: object) -> dict:
        calls.append(str(item["pdf_path"]))
        return {
            "schema_version": qa.SCHEMA_VERSION,
            "record_key": item["id"],
            "status": "ok",
            "source": {"pdf_path": item["pdf_path"], "sha256": qa._sha256(pdf)},
        }

    monkeypatch.setattr(qa, "analyze_record", fake_analyze)
    first = qa.run_batch(input_path, output_path, pdftotext_executable=None)
    assert first["counts"] == {"checkpoint_tail_repaired": 0, "ok": 1, "error": 1}
    second = qa.run_batch(input_path, output_path, pdftotext_executable=None)
    assert second["counts"]["skipped_unchanged"] == 1
    assert second["counts"]["error"] == 1
    assert len(calls) == 1

    # Same size and mtime are insufficient: changing content forces a fresh analysis.
    before = pdf.stat()
    pdf.write_bytes(b"%PDF-dummy-B")
    assert pdf.stat().st_size == before.st_size
    pdf.touch()
    third = qa.run_batch(input_path, output_path, pdftotext_executable=None)
    assert third["counts"]["ok"] == 1
    assert len(calls) == 2
    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert any(
        record["status"] == "error" and record["record_key"] == "missing" for record in records
    )


def test_checkpoint_only_repairs_unterminated_invalid_tail(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"record_key":"ok"}\n{"broken":', encoding="utf-8")
    records, repaired = qa._read_journal(journal)
    assert repaired is True
    assert records == [{"record_key": "ok"}]
    journal.write_text('{"record_key":"ok"}\n{"broken":\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        qa._read_journal(journal)


def test_prepare_metadata_video_mineru_and_recovery(tmp_path: Path) -> None:
    embodied_pdf = tmp_path / "Embodied" / "ACL 2026 AgentOCR_ Optical.pdf"
    video_pdf = tmp_path / "Video" / "ACL 2026 TVS_ Temporal Screening.pdf"
    recovered_pdf = tmp_path / "recovered" / "missing-01-2511.14582.pdf"
    for path in (embodied_pdf, video_pdf, recovered_pdf):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-test")
    metadata_path = tmp_path / "metadata.jsonl"
    metadata_records = [
        {
            "source": {"path": str(embodied_pdf), "sha256": "a" * 64},
            "embedded_metadata": {"/Title": "AgentOCR: Optical", "/Author": "Ada Lovelace"},
            "facts": {},
        },
        {
            "source": {"path": str(video_pdf), "sha256": "b" * 64},
            "embedded_metadata": {"/Title": "", "/Author": ""},
            "facts": {
                "title": {"value": "Temporal Screening"},
                "authors": {"value": ["Grace Hopper", "Alan Turing"]},
            },
        },
    ]
    metadata_path.write_text(
        "".join(json.dumps(record) + "\n" for record in metadata_records), encoding="utf-8"
    )
    video_root = tmp_path / "Video"
    mineru_root = video_root / "work" / "mineru"
    mineru_root.mkdir(parents=True)
    present_md = mineru_root / "ACL 2026 TVS_ Temporal Screening_1-20.md"
    present_md.write_text("# Temporal Screening\n", encoding="utf-8")
    recovered_md = mineru_root / "CVPR 2026 OmniZip.md"
    recovered_md.write_text("# OmniZip\n", encoding="utf-8")
    recovery_map = tmp_path / "video-recovery-map.json"
    recovery_map.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "index": 1,
                        "title": "OmniZip: Audio-Guided Compression",
                        "markdown": "work/mineru/CVPR 2026 OmniZip.md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "qa-input.jsonl"
    summary = prepare_inputs(
        metadata_path,
        output,
        video_mineru_root=mineru_root,
        recovery_map=recovery_map,
        recovered_pdf_root=recovered_pdf.parent,
        expected_count=3,
    )
    items = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert summary == {
        "metadata_latest": 2,
        "recovery_added": 1,
        "recovery_enriched": 0,
        "total": 3,
        "unique_ids": 3,
        "video_with_mineru": 2,
        "output": str(output),
    }
    assert items[0]["metadata_title"] == "AgentOCR: Optical"
    assert "mineru_path" not in items[0]
    assert items[1]["metadata_authors"] == "Grace Hopper, Alan Turing"
    assert items[1]["mineru_path"] == str(present_md.resolve())
    assert items[2]["reference_title"] == "OmniZip: Audio-Guided Compression"
    assert items[2]["mineru_path"] == str(recovered_md.resolve())
    assert len({item["id"] for item in items}) == 3
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        prepare_inputs(metadata_path, output, expected_count=2)


def test_prepare_fails_count_and_does_not_create_output(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = tmp_path / "metadata.jsonl"
    metadata.write_text(
        json.dumps({"source": {"path": str(pdf), "sha256": "c" * 64}}) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.jsonl"
    with pytest.raises(ValueError, match="expected 2 QA items, prepared 1"):
        prepare_inputs(metadata, output, expected_count=2)
    assert not output.exists()


def test_prepare_preserves_nfkc_distinct_filename_spelling(tmp_path: Path) -> None:
    pdf = tmp_path / "PRIM：Token（已读）.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = tmp_path / "metadata.jsonl"
    metadata.write_text(
        json.dumps({"source": {"path": str(pdf), "sha256": "e" * 64}}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.jsonl"

    summary = prepare_inputs(metadata, output, expected_count=1)

    item = json.loads(output.read_text(encoding="utf-8"))
    assert summary["total"] == 1
    assert item["pdf_path"] == str(pdf)
    assert "：" in item["pdf_path"]
    assert "（已读）" in item["pdf_path"]


def test_prepare_enriches_recovery_already_present_in_metadata(tmp_path: Path) -> None:
    recovered_pdf = tmp_path / "missing-01-paper.pdf"
    recovered_pdf.write_bytes(b"%PDF-test")
    metadata = tmp_path / "metadata.jsonl"
    metadata.write_text(
        json.dumps({"source": {"path": str(recovered_pdf), "sha256": "d" * 64}}) + "\n",
        encoding="utf-8",
    )
    video_root = tmp_path / "Video"
    mineru = video_root / "work" / "mineru" / "paper.md"
    mineru.parent.mkdir(parents=True)
    mineru.write_text("# Official Paper Title\n", encoding="utf-8")
    recovery_map = tmp_path / "recovery-map.json"
    recovery_map.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "index": 1,
                        "title": "Official Paper Title",
                        "markdown": "work/mineru/paper.md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out.jsonl"
    summary = prepare_inputs(
        metadata,
        output,
        video_mineru_root=video_root,
        recovery_map=recovery_map,
        recovered_pdf_root=tmp_path,
        expected_count=1,
    )
    item = json.loads(output.read_text(encoding="utf-8"))
    assert summary["recovery_added"] == 0
    assert summary["recovery_enriched"] == 1
    assert summary["total"] == 1
    assert item["reference_title"] == "Official Paper Title"
    assert item["recovery_map_id"] == "video-recovered-01"
