from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    RectangleObject,
)

from tools.paper_analysis_evidence.__main__ import main
from tools.paper_analysis_evidence.evidence import (
    CATEGORY_KEYS,
    EvidenceError,
    extract_record,
    load_paper_records,
    run_batch,
)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_text_pdf(
    path: Path,
    pages: list[list[str]],
    *,
    annotation_url: str | None = None,
) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)  # noqa: SLF001 - compact deterministic test fixture
    for lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        content = DecodedStreamObject()
        commands = ["BT", "/F1 10 Tf", "72 740 Td", "13 TL"]
        for line in lines:
            commands.extend((f"({_pdf_escape(line)}) Tj", "T*"))
        commands.append("ET")
        content.set_data(("\n".join(commands) + "\n").encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(content)  # noqa: SLF001
    if annotation_url:
        writer.add_uri(2, annotation_url, RectangleObject((10, 10, 240, 40)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer.write(stream)
    assert len(PdfReader(str(path)).pages) == len(pages)


def _fixture_pages() -> list[list[str]]:
    return [
        [
            "Abstract",
            "We study compact agents and preserve the source wording for evidence extraction.",
            "The method reduces redundant tokens while retaining task performance.",
        ],
        [
            "1 Introduction",
            "Efficient embodied systems must operate under bounded memory.",
            "We introduce a deterministic compression module as our main contribution.",
        ],
        [
            "3 Method",
            "The encoder ranks observations and retains a compact set before policy inference.",
            "Code is available at https://user:secret@github.com/example/compact-agent?token=private.",
        ],
        [
            "4 Experiments",
            "The model achieves 72.5% success and reduces memory by 35% on the test split.",
        ],
        [
            "6 Limitations",
            "The evaluation is limited to indoor simulation and does not cover physical robots.",
            "7 Conclusion",
            "The paper concludes with evidence from the reported benchmarks.",
        ],
    ]


def _write_markdown(path: Path, *, method_word: str = "ranks") -> None:
    path.write_text(
        """<!-- Page 1 -->
# Abstract
We study compact agents and preserve the source wording for evidence extraction.

<!-- Page 2 -->
# 1 Introduction
Efficient embodied systems must operate under bounded memory. We introduce a deterministic
compression module as our main contribution.

<!-- Page 3 -->
# 3 Method
The encoder METHOD_WORD observations and retains a compact set before policy inference.
Code is available at https://user:secret@github.com/example/compact-agent?token=private.

<!-- Page 4 -->
# 4 Experiments
The model achieves 72.5% success and reduces memory by 35% on the test split.

<!-- Page 5 -->
# 6 Limitations
The evaluation is limited to indoor simulation and does not cover physical robots.
""".replace("METHOD_WORD", method_word),
        encoding="utf-8",
        newline="\n",
    )


def _record(
    pdf: Path,
    markdown: Path | None = None,
    *,
    paper_id: str = "compact-agent-2026",
) -> dict:
    record: dict = {
        "paper_id": paper_id,
        "title": "Compact Agent Evidence",
        "title_zh": "紧凑智能体证据",
        "year": 2026,
        "pdf": {"path": pdf.name},
    }
    if markdown:
        record["mineru_markdown"] = {"path": markdown.name}
    return record


def test_markdown_sections_produce_page_located_verbatim_evidence(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    markdown = tmp_path / "paper.md"
    _write_text_pdf(
        pdf,
        _fixture_pages(),
        annotation_url="https://github.com/example/compact-agent?download=1",
    )
    _write_markdown(markdown)

    payload, fingerprint = extract_record(_record(pdf, markdown), base_dir=tmp_path)

    assert payload["status"] == "ok"
    assert len(fingerprint) == 64
    assert payload["policy"]["generated_summary_present"] is False
    assert payload["coverage"]["abstract"] == 1
    assert payload["coverage"]["method"] >= 1
    assert payload["coverage"]["experiments_results"] >= 1
    assert payload["coverage"]["limitations_conclusion"] >= 1
    for category in CATEGORY_KEYS:
        for item in payload["evidence"][category]:
            assert isinstance(item["page"], int) and item["page"] >= 1
            assert item["section"]
            assert item["excerpt"]
            assert len(item["excerpt_sha256"]) == 64

    code_items = payload["evidence"]["code_urls"]
    assert code_items
    assert all(item["verification"] == "candidate_unverified" for item in code_items)
    assert any(item["url"] == "https://github.com/example/compact-agent" for item in code_items)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "secret" not in serialized
    assert "token=private" not in serialized


def test_pdf_text_is_a_fallback_when_no_mineru_markdown_exists(tmp_path: Path) -> None:
    pdf = tmp_path / "fallback.pdf"
    _write_text_pdf(pdf, _fixture_pages())

    payload, _ = extract_record(_record(pdf), base_dir=tmp_path)

    assert payload["coverage"]["abstract"] == 1
    assert payload["evidence"]["abstract"][0]["source_kind"] == "pdf_text"
    result_excerpt = payload["evidence"]["experiments_results"][0]["excerpt"]
    assert "72.5%" in result_excerpt
    assert payload["evidence"]["limitations_conclusion"][0]["page"] == 5


def test_per_paper_character_limit_is_enforced(tmp_path: Path) -> None:
    pdf = tmp_path / "limit.pdf"
    pages = _fixture_pages()
    pages[2].append("Method detail " * 500)
    _write_text_pdf(pdf, pages)

    payload, _ = extract_record(
        _record(pdf),
        base_dir=tmp_path,
        max_evidence_chars=1_000,
        max_excerpt_chars=240,
        max_items_per_category=2,
    )

    actual = sum(
        len(item["excerpt"])
        for category in CATEGORY_KEYS
        for item in payload["evidence"][category]
    )
    assert actual == payload["total_evidence_chars"]
    assert actual <= 1_000
    assert all(
        len(item["excerpt"]) <= 240
        for category in CATEGORY_KEYS
        for item in payload["evidence"][category]
    )


def test_batch_checkpoint_uses_content_hash_and_keeps_per_file_error(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    markdown = tmp_path / "paper.md"
    _write_text_pdf(pdf, _fixture_pages())
    _write_markdown(markdown, method_word="ranks")
    missing = tmp_path / "missing.pdf"
    records = [
        _record(pdf, markdown),
        {
            "paper_id": "missing-paper-2026",
            "title": "Missing Paper",
            "pdf": {"path": missing.name},
        },
    ]
    output = tmp_path / "evidence"

    first = run_batch(records, base_dir=tmp_path, output_dir=output)
    second = run_batch(records, base_dir=tmp_path, output_dir=output)
    assert first["processed"] == 2
    assert first["failed"] == 1
    assert second["processed"] == 1
    assert second["skipped_unchanged"] == 1
    error_payload = json.loads(
        (output / "missing-paper-2026.evidence.json").read_text(encoding="utf-8")
    )
    assert error_payload["status"] == "error"
    assert error_payload["error"]["type"] == "EvidenceError"

    old_stat = markdown.stat()
    before_bytes = markdown.read_bytes()
    _write_markdown(markdown, method_word="sorts")
    assert len(markdown.read_bytes()) == len(before_bytes)
    os.utime(markdown, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    third = run_batch([records[0]], base_dir=tmp_path, output_dir=output)
    assert third["processed"] == 1
    assert third["skipped_unchanged"] == 0
    changed = json.loads((output / "compact-agent-2026.evidence.json").read_text("utf-8"))
    assert "sorts observations" in changed["evidence"]["method"][0]["excerpt"]


def test_loader_accepts_wrapped_utf8_records_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    source = tmp_path / "records.json"
    source.write_text(
        json.dumps(
            {
                "papers": [
                    {"paper_id": "paper-one-2026", "title_zh": "论文一"},
                    {"id": "paper-two-2026", "title_zh": "论文二"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )
    records = load_paper_records(source)
    assert [record["paper_id"] for record in records] == ["paper-one-2026", "paper-two-2026"]

    source.write_text(
        json.dumps([{"paper_id": "same-id"}, {"paper_id": "same-id"}]),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="重复 paper_id"):
        load_paper_records(source)


def test_unpageable_scanned_pdf_is_an_explicit_error(tmp_path: Path) -> None:
    pdf = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(EvidenceError, match="未提取到带 PDF 页码"):
        extract_record(_record(pdf), base_dir=tmp_path)


def test_cli_small_sample_writes_utf8_report_and_checkpoint(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    markdown = tmp_path / "paper.md"
    records_path = tmp_path / "records.json"
    output = tmp_path / "output"
    _write_text_pdf(pdf, _fixture_pages())
    _write_markdown(markdown)
    records_path.write_text(
        json.dumps(_record(pdf, markdown), ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--input",
            str(records_path),
            "--output",
            str(output),
            "--max-evidence-chars",
            "2000",
        ]
    )

    assert exit_code == 0
    assert (output / "checkpoint.json").is_file()
    report = json.loads((output / "batch-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    evidence_bytes = (output / "compact-agent-2026.evidence.json").read_bytes()
    assert "紧凑智能体证据".encode() in evidence_bytes
    schema = json.loads(
        (Path(__file__).parents[1] / "evidence.schema.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"]["const"] == "paper-analysis-evidence.v1"


def test_intake_builder_pdf_path_priority_and_mineru_path_compatibility(tmp_path: Path) -> None:
    target_pdf = tmp_path / "target" / "paper.pdf"
    source_pdf = tmp_path / "source" / "paper.pdf"
    fallback_pdf = tmp_path / "record" / "paper.pdf"
    markdown = tmp_path / "mineru" / "paper.md"
    for path in (target_pdf, source_pdf, fallback_pdf):
        _write_text_pdf(path, _fixture_pages())
    markdown.parent.mkdir(parents=True)
    _write_markdown(markdown)

    record = _record(fallback_pdf)
    record.update(
        {
            "target_pdf_path": str(target_pdf),
            "source_pdf_path": str(source_pdf),
            "mineru_markdown_path": str(markdown),
        }
    )
    payload, _ = extract_record(record, base_dir=tmp_path)
    assert payload["sources"]["pdf"]["path"] == str(target_pdf.resolve())
    assert payload["sources"]["mineru_markdown"]["path"] == str(markdown.resolve())

    source_fallback_record = _record(fallback_pdf, paper_id="source-fallback-2026")
    source_fallback_record.update(
        {
            "target_pdf_path": str(tmp_path / "missing-target.pdf"),
            "source_pdf_path": str(source_pdf),
        }
    )
    source_payload, _ = extract_record(source_fallback_record, base_dir=tmp_path)
    assert source_payload["sources"]["pdf"]["path"] == str(source_pdf.resolve())

    pdf_path_fallback_record = _record(fallback_pdf, paper_id="pdf-path-fallback-2026")
    pdf_path_fallback_record.update(
        {
            "target_pdf_path": str(tmp_path / "missing-target.pdf"),
            "source_pdf_path": str(tmp_path / "missing-source.pdf"),
        }
    )
    fallback_payload, _ = extract_record(pdf_path_fallback_record, base_dir=fallback_pdf.parent)
    assert fallback_payload["sources"]["pdf"]["path"] == str(fallback_pdf.resolve())
