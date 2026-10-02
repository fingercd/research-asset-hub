from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import RectangleObject

from tools.paper_metadata.extract import (
    ARXIV_RE,
    DEFAULT_FACT_THRESHOLD,
    DOI_RE,
    _abstract_candidates,
    _code_leads,
    _facts,
    _identifier_candidates,
    _metadata_dict,
    _read_journal,
    _xmp_dict,
    clean_text,
    clean_url,
    extract_pdf,
    run,
    scan_inputs,
)


def test_clean_text_replaces_lone_surrogate_for_utf8_outputs() -> None:
    cleaned = clean_text("before\ud835after")
    assert cleaned == "before?after"
    assert cleaned.encode("utf-8")


def _write_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": "Reliable Compression for Embodied Navigation",
            "/Author": "Ada Example; Lin Tester",
            "/Subject": (
                "This sufficiently long metadata description is intentionally a candidate "
                "but stays below the default fact threshold because a PDF subject is not "
                "guaranteed to be an abstract."
            ),
            "/DOI": "doi:10.1234/example.2026",
            "/ArXiv": "arXiv:2608.12345",
        }
    )
    writer.add_uri(
        0,
        "https://github.com/example/reliable-compression",
        RectangleObject((10, 10, 200, 40)),
    )
    with path.open("wb") as handle:
        writer.write(handle)


def test_extract_metadata_identifiers_annotations_and_fact_gate(tmp_path: Path) -> None:
    pdf = tmp_path / "fixture.pdf"
    _write_fixture(pdf)
    record = extract_pdf(pdf)

    assert record["status"] == "ok"
    assert record["facts"]["title"]["value"] == "Reliable Compression for Embodied Navigation"
    assert record["facts"]["authors"]["value"] == ["Ada Example", "Lin Tester"]
    assert record["facts"]["doi"][0]["value"] == "10.1234/example.2026"
    assert record["facts"]["arxiv_id"][0]["value"] == "2608.12345"
    assert "abstract" not in record["facts"]
    assert record["candidates"]["abstract"][0]["confidence"] < DEFAULT_FACT_THRESHOLD
    assert record["code_leads"][0]["url"] == "https://github.com/example/reliable-compression"
    assert record["code_leads"][0]["status"] == "candidate_unverified"
    assert record["code_leads"][0]["evidence"][0]["source"] == "annotation_uri"


def test_text_candidate_helpers_keep_evidence_and_detect_labeled_abstract() -> None:
    text = (
        "CVPR 2026\nAbstract\n"
        "We introduce a reproducible compression method for embodied agents. "
        "It reduces memory and latency while preserving navigation success across "
        "several benchmarks.\n"
        "1 Introduction\nCode is available at https://gitlab.com/lab/project."
    )
    abstracts = _abstract_candidates(text, {}, {})
    assert abstracts[0]["confidence"] >= DEFAULT_FACT_THRESHOLD
    assert abstracts[0]["evidence"][0]["source"] == "first_pages_text"

    identifiers = _identifier_candidates(
        "doi",
        DOI_RE,
        [("text", "x", "doi:10.5555/ABC")],
    )
    assert identifiers[0]["value"] == "10.5555/abc"

    leads, urls = _code_leads(text, [])
    assert "https://gitlab.com/lab/project" in urls
    assert any(lead.get("url") == "https://gitlab.com/lab/project" for lead in leads)
    assert all(lead["status"] == "candidate_unverified" for lead in leads)


def test_run_is_resumable_and_csv_is_current_snapshot(tmp_path: Path) -> None:
    pdf = tmp_path / "papers" / "fixture.pdf"
    _write_fixture(pdf)
    prefix = tmp_path / "reports" / "metadata"

    first = run([pdf.parent], prefix)
    second = run([pdf.parent], prefix)
    assert first["processed"] == 1
    assert second["processed"] == 0
    assert second["skipped_unchanged"] == 1

    jsonl = prefix.with_suffix(".jsonl")
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert prefix.with_suffix(".csv").exists()

    with jsonl.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"truncated"')
    repaired = run([pdf.parent], prefix)
    assert repaired["repaired_truncated_jsonl_lines"] == 1
    assert repaired["skipped_unchanged"] == 1
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 1


def test_previous_error_remains_visible_when_resume_skips_it(tmp_path: Path) -> None:
    invalid_pdf = tmp_path / "invalid.pdf"
    invalid_pdf.write_bytes(b"%PDF-1.7\nnot a valid PDF")
    prefix = tmp_path / "failure-report"

    first = run([invalid_pdf], prefix)
    second = run([invalid_pdf], prefix)
    assert first["processed_failures"] == 1
    assert first["failures"] == 1
    assert second["processed"] == 0
    assert second["failures"] == 1


def test_identifier_links_on_later_pages_are_candidates_not_identity_facts(tmp_path: Path) -> None:
    pdf = tmp_path / "references.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    writer.add_uri(
        1,
        "https://doi.org/10.48550/arXiv.2505.09388",
        RectangleObject((10, 10, 200, 40)),
    )
    writer.add_uri(
        1,
        "https://arxiv.org/abs/2505.09388",
        RectangleObject((10, 50, 200, 80)),
    )
    with pdf.open("wb") as handle:
        writer.write(handle)

    record = extract_pdf(pdf)
    assert record["candidates"]["doi"][0]["confidence"] < DEFAULT_FACT_THRESHOLD
    assert record["candidates"]["arxiv_id"][0]["confidence"] < DEFAULT_FACT_THRESHOLD
    assert "doi" not in record["facts"]
    assert "arxiv_id" not in record["facts"]


def test_conflicting_high_confidence_singular_values_are_not_asserted() -> None:
    facts, conflicts = _facts(
        {
            "title": [
                {"value": "Paper A", "confidence": 0.9},
                {"value": "Paper B", "confidence": 0.88},
            ]
        },
        DEFAULT_FACT_THRESHOLD,
    )
    assert "title" not in facts
    assert conflicts == ["title"]

    identifier_facts, _ = _facts(
        {"doi": [{"value": "10.5555/unqualified", "confidence": 1.0}]},
        0.0,
    )
    assert "doi" not in identifier_facts


def test_untrusted_identifier_urls_and_random_filenames_never_become_facts(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "paper-2020.12345.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_uri(
        0,
        "https://user:secret@evil.example/doi:10.1111/not-a-doi?token=private#fragment",
        RectangleObject((10, 10, 200, 40)),
    )
    writer.add_uri(
        0,
        "https://evil.example/10.2222/no-explicit-prefix",
        RectangleObject((10, 50, 200, 80)),
    )
    writer.add_uri(
        0,
        "https://evilgithub.io/project?access_token=private",
        RectangleObject((10, 90, 200, 120)),
    )
    writer.add_uri(
        0,
        "https://evil.example/arxiv:2020.54321?token=private",
        RectangleObject((10, 130, 200, 160)),
    )
    with pdf.open("wb") as handle:
        writer.write(handle)

    record = extract_pdf(pdf, fact_threshold=0.0)
    serialized = json.dumps(record, ensure_ascii=False)
    assert "doi" not in record["facts"]
    assert "arxiv_id" not in record["facts"]
    assert [item["value"] for item in record["candidates"]["doi"]] == ["10.1111/not-a-doi"]
    assert all(not item["fact_eligible"] for item in record["candidates"]["doi"])
    assert [item["value"] for item in record["candidates"]["arxiv_id"]] == ["2020.54321"]
    assert all(not item["fact_eligible"] for item in record["candidates"]["arxiv_id"])
    assert all(item["value"] != "2020.12345" for item in record["candidates"]["arxiv_id"])
    assert not record["code_leads"]
    assert "secret" not in serialized
    assert "token=" not in serialized
    assert "private" not in serialized
    assert clean_url("https://u:p@github.com/owner/repo?q=secret#x") == (
        "https://github.com/owner/repo"
    )
    assert clean_url("https://u:p@github.com:8443/owner/repo?q=secret#x") == (
        "https://github.com/owner/repo"
    )
    assert not DOI_RE.search("a bare identifier 10.1111/example.2026")
    assert not ARXIV_RE.search("x2020.12345y")
    assert DOI_RE.search("doi:10.1111/example.2026.").group(1) == "10.1111/example.2026"
    assert DOI_RE.search("doi:10.1111/example.2026).").group(1) == "10.1111/example.2026"
    assert ARXIV_RE.search("https://arxiv.org/abs/2020.12345.").group(1) == "2020.12345"
    assert not ARXIV_RE.search("arXiv:2020.12345.999")


def test_arbitrary_metadata_and_cross_identifier_urls_are_never_identity_facts(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "metadata-confusion.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": "Identifier Confusion Fixture",
            "/Keywords": "doi:10.9999/not-trusted arXiv:2020.12345",
        }
    )
    writer.add_uri(
        0,
        "https://doi.org/10.1000/foo.2020.12345",
        RectangleObject((10, 10, 200, 40)),
    )
    writer.add_uri(
        0,
        "https://doi.org/foo/doi:10.8888/nested-fake",
        RectangleObject((10, 50, 200, 80)),
    )
    writer.add_uri(
        0,
        "https://arxiv.org/abs/foo/arxiv:2020.54321",
        RectangleObject((10, 90, 200, 120)),
    )
    with pdf.open("wb") as handle:
        writer.write(handle)

    record = extract_pdf(pdf, fact_threshold=0.0)
    assert [item["value"] for item in record["facts"]["doi"]] == ["10.1000/foo.2020.12345"]
    assert "arxiv_id" not in record["facts"]
    assert any(
        item["value"] == "10.9999/not-trusted" and not item["fact_eligible"]
        for item in record["candidates"]["doi"]
    )
    assert any(
        item["value"] == "2020.12345" and not item["fact_eligible"]
        for item in record["candidates"]["arxiv_id"]
    )
    assert any(
        item["value"] == "10.8888/nested-fake" and not item["fact_eligible"]
        for item in record["candidates"]["doi"]
    )
    assert any(
        item["value"] == "2020.54321" and not item["fact_eligible"]
        for item in record["candidates"]["arxiv_id"]
    )


def test_metadata_and_xmp_keys_are_url_sanitized() -> None:
    secret_key = "/https://user:password@evil.example/key?token=private#fragment"

    class MetadataReader:
        metadata = {secret_key: "value"}

    class Xmp:
        dc_title = {secret_key: "value"}

    class XmpReader:
        xmp_metadata = Xmp()

    metadata, metadata_warnings = _metadata_dict(MetadataReader())  # type: ignore[arg-type]
    xmp, xmp_warnings = _xmp_dict(XmpReader())  # type: ignore[arg-type]
    serialized = json.dumps(
        {
            "metadata": metadata,
            "metadata_warnings": metadata_warnings,
            "xmp": xmp,
            "xmp_warnings": xmp_warnings,
        }
    )

    assert "password" not in serialized
    assert "token=" not in serialized
    assert "private" not in serialized
    assert "https://evil.example/key" in serialized


def test_untrusted_external_code_urls_are_not_code_leads_or_leaked() -> None:
    text = (
        "Code is available at "
        "https://user:password@evilgithub.io/project?access_token=private#fragment"
    )
    leads, urls = _code_leads(text, [])
    serialized = json.dumps({"leads": leads, "urls": urls})

    assert not leads
    assert urls == ["https://evilgithub.io/project"]
    assert "password" not in serialized
    assert "access_token" not in serialized
    assert "private" not in serialized

    trusted_leads, _ = _code_leads(
        "Code is available at https://owner.github.io/project?token=private", []
    )
    statement_lead = next(
        lead for lead in trusted_leads if lead["type"] == "code_availability_statement"
    )
    assert statement_lead["urls"] == ["https://owner.github.io/project"]
    assert "private" not in json.dumps(trusted_leads)


def test_resume_compares_sha_even_when_size_and_mtime_match(tmp_path: Path) -> None:
    pdf = tmp_path / "fixture.pdf"
    _write_fixture(pdf)
    prefix = tmp_path / "fingerprint"
    first = run([pdf], prefix)
    first_sha = json.loads(prefix.with_suffix(".jsonl").read_text(encoding="utf-8"))["source"][
        "sha256"
    ]
    original_stat = pdf.stat()
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF-1.")
    replacement = b"4" if data[7:8] != b"4" else b"3"
    pdf.write_bytes(data[:7] + replacement + data[8:])
    os.utime(pdf, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    second = run([pdf], prefix)
    records = [
        json.loads(line)
        for line in prefix.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert first["processed"] == 1
    assert second["processed"] == 1
    assert second["skipped_unchanged"] == 0
    assert records[-1]["source"]["sha256"] != first_sha


def test_resume_does_not_skip_when_source_changes_after_first_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "fixture.pdf"
    _write_fixture(pdf)
    prefix = tmp_path / "hash-race"
    run([pdf], prefix)
    original_stat = pdf.stat()
    original_data = pdf.read_bytes()
    replacement_byte = b"4" if original_data[7:8] != b"4" else b"3"
    replacement_data = original_data[:7] + replacement_byte + original_data[8:]

    from tools.paper_metadata import extract as extract_module

    original_hash_file = extract_module.hash_file
    replaced = False

    def hash_then_replace(path: Path) -> str:
        nonlocal replaced
        digest = original_hash_file(path)
        if not replaced and path == pdf.resolve():
            replacement = tmp_path / "replacement.pdf"
            replacement.write_bytes(replacement_data)
            os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            os.replace(replacement, pdf)
            replaced = True
        return digest

    monkeypatch.setattr(extract_module, "hash_file", hash_then_replace)
    second = run([pdf], prefix)
    latest = json.loads(prefix.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert replaced
    assert second["skipped_unchanged"] == 0
    assert second["processed"] == 1
    assert latest["source"]["sha256"] == original_hash_file(pdf)


def test_explicit_reparse_input_is_rejected_before_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "papers"
    root.mkdir()
    resolved = root.resolve()
    monkeypatch.setattr(
        "tools.paper_metadata.extract._is_reparse_point",
        lambda path: path == root.absolute(),
    )
    monkeypatch.setattr(Path, "resolve", lambda self, strict=True: resolved)

    with pytest.raises(ValueError, match="reparse point"):
        scan_inputs([root])


def test_reparse_ancestor_is_rejected_before_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked_parent = tmp_path / "linked-parent"
    nested = linked_parent / "papers"
    nested.mkdir(parents=True)
    resolved_called = False
    original_resolve = Path.resolve

    monkeypatch.setattr(
        "tools.paper_metadata.extract._is_reparse_point",
        lambda path: path == linked_parent.absolute(),
    )

    def guarded_resolve(self: Path, strict: bool = False) -> Path:
        nonlocal resolved_called
        resolved_called = True
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with pytest.raises(ValueError, match="reparse point"):
        scan_inputs([nested])
    assert not resolved_called


def test_per_file_resolve_error_is_recorded_and_batch_continues(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.pdf"
    missing = tmp_path / "vanished.pdf"
    _write_fixture(valid)
    result = run([missing, valid], tmp_path / "per-file")
    rows = list(csv.DictReader((tmp_path / "per-file.csv").open(encoding="utf-8-sig")))
    assert result["current_records"] == 2
    assert result["failures"] == 1
    assert {row["status"] for row in rows} == {"error", "ok"}


def test_file_disappearing_after_scan_is_recorded_and_batch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disappearing = tmp_path / "disappearing.pdf"
    valid = tmp_path / "valid.pdf"
    _write_fixture(disappearing)
    _write_fixture(valid)
    original_scan = scan_inputs

    def scan_then_remove(inputs: list[Path]) -> list[Path]:
        discovered = original_scan(inputs)
        disappearing.unlink()
        return discovered

    monkeypatch.setattr("tools.paper_metadata.extract.scan_inputs", scan_then_remove)
    result = run([tmp_path], tmp_path / "post-scan-disappearance")
    rows = list(
        csv.DictReader((tmp_path / "post-scan-disappearance.csv").open(encoding="utf-8-sig"))
    )

    assert result["current_records"] == 2
    assert result["failures"] == 1
    assert sorted(row["status"] for row in rows) == ["error", "ok"]
    error_row = next(row for row in rows if row["status"] == "error")
    assert json.loads(error_row["error_json"])["stage"] == "resolve"


def test_scan_resolve_and_run_stat_errors_are_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_root = tmp_path / "blocked-root"
    blocked_root.mkdir()
    blocked_pdf = tmp_path / "blocked-stat.pdf"
    valid = tmp_path / "valid.pdf"
    _write_fixture(blocked_pdf)
    _write_fixture(valid)

    original_resolve = Path.resolve
    original_stat = Path.stat
    blocked_root_key = os.path.normcase(str(blocked_root.absolute()))
    blocked_pdf_key = os.path.normcase(str(blocked_pdf.absolute()))

    def guarded_resolve(self: Path, strict: bool = False) -> Path:
        if os.path.normcase(str(self.absolute())) == blocked_root_key:
            raise PermissionError("root resolve denied")
        return original_resolve(self, strict=strict)

    def guarded_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if os.path.normcase(str(self.absolute())) == blocked_pdf_key:
            raise PermissionError("file stat denied")
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    result = run([blocked_root, blocked_pdf, valid], tmp_path / "permission-errors")
    rows = list(csv.DictReader((tmp_path / "permission-errors.csv").open(encoding="utf-8-sig")))

    assert result["current_records"] == 3
    assert result["failures"] == 2
    assert sorted(row["status"] for row in rows) == ["error", "error", "ok"]
    error_stages = {
        json.loads(row["error_json"])["stage"] for row in rows if row["status"] == "error"
    }
    assert error_stages == {"resolve", "stat"}


def test_scan_runtime_resolve_error_is_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "loop.pdf"
    valid = tmp_path / "valid.pdf"
    _write_fixture(blocked)
    _write_fixture(valid)
    original_resolve = Path.resolve
    blocked_key = os.path.normcase(str(blocked.absolute()))

    def guarded_resolve(self: Path, strict: bool = False) -> Path:
        if os.path.normcase(str(self.absolute())) == blocked_key:
            raise RuntimeError("simulated symlink loop")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    result = run([blocked, valid], tmp_path / "runtime-resolve")
    rows = list(csv.DictReader((tmp_path / "runtime-resolve.csv").open(encoding="utf-8-sig")))

    assert result["current_records"] == 2
    assert result["failures"] == 1
    assert sorted(row["status"] for row in rows) == ["error", "ok"]


def test_extract_pdf_resolve_error_is_a_visible_record(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pdf"
    record = extract_pdf(missing)
    assert record["status"] == "error"
    assert record["error"]["stage"] == "resolve"


def test_newline_terminated_invalid_journal_tail_is_not_silently_repaired(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "bad.jsonl"
    journal.write_text('{"valid": true}\n{"broken"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        _read_journal(journal)
    assert journal.read_text(encoding="utf-8").endswith('{"broken"\n')


def test_csv_snapshot_excludes_paths_not_in_current_scan(tmp_path: Path) -> None:
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    _write_fixture(first_pdf)
    _write_fixture(second_pdf)
    prefix = tmp_path / "scope"
    run([tmp_path], prefix)

    result = run([first_pdf], prefix)
    with prefix.with_suffix(".csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert result["current_records"] == 1
    assert len(rows) == 1
    assert Path(rows[0]["source_path"]) == first_pdf.resolve()
    assert len(prefix.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()) == 2
