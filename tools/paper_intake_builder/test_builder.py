from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from tools.paper_intake_builder.builder import (
    BuildResult,
    apply_author_resolution,
    apply_copy_plan,
    build_intake,
    load_routing_decisions,
    route_domain,
    sha256_file,
    stable_paper_id,
    write_outputs,
)


def make_pdf(path: Path, *, pages: int = 1, marker: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": marker})
    with path.open("wb") as handle:
        writer.write(handle)


def metadata_record(
    path: Path,
    *,
    title: str,
    year: int,
    venue: str,
    arxiv_id: str | None = None,
) -> dict[str, object]:
    facts: dict[str, object] = {
        "title": {"value": title},
        "authors": {"value": ["Ada Example", "Bo Tester"]},
        "year": {"value": year},
        "venue": {"value": venue},
    }
    if arxiv_id:
        facts["arxiv_id"] = [{"value": arxiv_id}]
    return {
        "schema_version": "paper-metadata/v1",
        "status": "ok",
        "source": {
            "path": str(path),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        },
        "parser": {"page_count": 1},
        "facts": facts,
        "warnings": [],
    }


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_empty_recovery(path: Path) -> None:
    path.write_text(json.dumps({"records": []}), encoding="utf-8")


def write_catalog(path: Path, papers: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"papers": papers}), encoding="utf-8")


def hold_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, str]]:
    desktop = tmp_path / "desktop"
    source = desktop / "Video" / "CVPR 2026 Versioned_ Efficient Video LLM.pdf"
    make_pdf(source, marker="new-version")
    metadata = tmp_path / "metadata.jsonl"
    write_jsonl(
        metadata,
        [
            metadata_record(
                source,
                title="Versioned: Efficient Video LLM",
                year=2026,
                venue="CVPR",
            )
        ],
    )
    recovery = tmp_path / "recovery.json"
    write_empty_recovery(recovery)
    core = tmp_path / "core"
    existing = (
        core
        / "VLM框架3DV压缩"
        / "library"
        / "papers"
        / "versioned-vlm-2026"
        / "paper.pdf"
    )
    make_pdf(existing, marker="old-version")
    catalog = core / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(
        catalog,
        [
            {
                "id": "versioned-vlm-2026",
                "title": "Versioned: Efficient Video LLM",
                "year": 2026,
                "pdf_local": "library/papers/versioned-vlm-2026/paper.pdf",
                "pdf_status": {"sha256": sha256_file(existing)},
            }
        ],
    )
    args: dict[str, object] = {
        "metadata_path": metadata,
        "recovery_map_path": recovery,
        "desktop_root": desktop,
        "vlm_catalog_path": catalog,
        "core_root": core,
        "expected_count": 1,
        "validate_batch_shape": False,
    }
    decision = {
        "source_sha256": sha256_file(source),
        "existing_vlm_id": "versioned-vlm-2026",
        "decision": "same_identity_alternate_version",
        "version_label": "desktop-intake-2026-08-31",
        "target_relative_path": (
            "library/papers/versioned-vlm-2026/versions/desktop-intake-2026-08-31.pdf"
        ),
        "evidence": ["Title, authors and abstract were manually compared."],
    }
    return args, decision


def test_stable_id_prefers_arxiv_then_method_year() -> None:
    assert (
        stable_paper_id(
            arxiv_id="arXiv:2601.01234v2",
            title="Ignored: Full title",
            year=2025,
            filename_method="Ignored",
        )
        == "arxiv-2601-01234"
    )
    assert (
        stable_paper_id(
            arxiv_id=None,
            title="Full Method Name: A Study",
            year=2026,
            filename_method="ShortMethod",
        )
        == "shortmethod-2026"
    )
    assert (
        stable_paper_id(
            arxiv_id=None,
            title="Accurate Quantized Video Diffusion Transformer",
            year=2025,
            filename_method="$_text{S}^2$Q-VDiT",
        )
        == "text-s-2-q-vdit-2025"
    )


def test_review_routing_is_auditable_and_conflicts_stay_in_review() -> None:
    generation = route_domain(
        "video-deleted-review",
        "Fast Diffusion Transformer for Video Generation",
        "paper.pdf",
    )
    assert generation["domain"] == "generation"
    assert generation["rule_ids"] == ["route-generation"]

    conflict = route_domain(
        "other-review",
        "Reinforcement Learning for a World Model",
        "paper.pdf",
    )
    assert conflict["domain"] == "needs-review"
    assert set(conflict["rule_ids"]) == {
        "route-world-model",
        "route-reinforcement-learning",
    }


def test_build_detects_existing_vlm_exact_hash_and_reuses(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    pdf = desktop / "Video" / "CVPR 2026 SampleMethod_ Efficient Video LLM.pdf"
    make_pdf(pdf, marker="sample")
    metadata = tmp_path / "metadata.jsonl"
    write_jsonl(
        metadata,
        [
            metadata_record(
                pdf,
                title="SampleMethod: Efficient Video LLM",
                year=2026,
                venue="CVPR",
                arxiv_id="2601.01234",
            )
        ],
    )
    recovery = tmp_path / "recovery.json"
    write_empty_recovery(recovery)
    core = tmp_path / "core"
    existing = core / "VLM框架3DV压缩" / "library" / "papers" / "sample-2026" / "paper.pdf"
    make_pdf(existing, marker="sample")
    assert sha256_file(existing) == sha256_file(pdf)
    catalog = core / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(
        catalog,
        [
            {
                "id": "sample-2026",
                "title": "SampleMethod: Efficient Video LLM",
                "year": 2026,
                "page": "https://arxiv.org/abs/2601.01234",
                "pdf_local": "library/papers/sample-2026/paper.pdf",
                "pdf_status": {"sha256": sha256_file(existing)},
            }
        ],
    )

    result = build_intake(
        metadata_path=metadata,
        recovery_map_path=recovery,
        desktop_root=desktop,
        vlm_catalog_path=catalog,
        core_root=core,
        expected_count=1,
        validate_batch_shape=False,
    )

    assert result.ready
    assert result.records[0]["paper_id"] == "arxiv-2601-01234"
    assert result.copy_actions[0]["planned_action"] == "reuse_existing_vlm"
    assert result.copy_actions[0]["target_path"] == str(existing.resolve())


def test_build_adds_recovered_map_record_and_uses_official_title(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    recovered = tmp_path / "core" / "recovered" / "missing-01-2602.01234.pdf"
    make_pdf(recovered, pages=2, marker="recovered")
    metadata = tmp_path / "metadata.csv"
    with metadata.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_path", "status"])
        writer.writeheader()
    recovery = tmp_path / "video-recovery-map.json"
    recovery.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "index": 1,
                        "title": "RecoveredMethod: Official Full Title",
                        "markdown": "work/mineru/CVPR 2026 RecoveredMethod.md",
                        "arxiv_id": "2602.01234",
                        "doi": None,
                        "official_landing_url": "https://arxiv.org/abs/2602.01234",
                        "official_pdf_url": "https://arxiv.org/pdf/2602.01234",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "video-download.json"
    report.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "index": 1,
                        "path": str(recovered),
                        "sha256": sha256_file(recovered),
                        "size": recovered.stat().st_size,
                        "pages": 2,
                        "status": "reused_validated",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog = tmp_path / "core" / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(catalog, [])

    result = build_intake(
        metadata_path=metadata,
        recovery_map_path=recovery,
        recovery_report_path=report,
        desktop_root=desktop,
        vlm_catalog_path=catalog,
        core_root=tmp_path / "core",
        expected_count=1,
        validate_batch_shape=False,
    )

    assert result.ready
    record = result.records[0]
    assert record["source_group"] == "video-main-recovered"
    assert record["title"] == "RecoveredMethod: Official Full Title"
    assert record["paper_id"] == "arxiv-2602-01234"
    assert record["pdf"]["page_count"] == 2


def test_explicit_identity_decision_copies_only_into_existing_versions(tmp_path: Path) -> None:
    args, decision = hold_fixture(tmp_path)
    decision_file = tmp_path / "identity-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "paper-intake-identity-decisions/v1",
                "decisions": [decision],
            }
        ),
        encoding="utf-8",
    )

    result = build_intake(**args, identity_decisions_path=decision_file)  # type: ignore[arg-type]

    assert result.ready
    assert result.report["identity_decisions"] == {
        "provided": 1,
        "applied": 1,
        "unresolved_holds": 0,
    }
    action = result.copy_actions[0]
    assert action["planned_action"] == "copy_alternate_version"
    assert action["existing_vlm_id"] == "versioned-vlm-2026"
    assert Path(action["target_path"]).parts[-2:] == (
        "versions",
        "desktop-intake-2026-08-31.pdf",
    )
    record = result.records[0]
    assert record["source_record_id"] == "versioned-2026"
    assert record["canonical_paper_id"] == "versioned-vlm-2026"
    assert record["existing_vlm_id"] == "versioned-vlm-2026"
    assert record["version_relation"]["relation_type"] == "alternate_version_of"
    assert record["version_relation"]["evidence"] == decision["evidence"]
    assert decision["evidence"][0] in record["decision_evidence"]

    apply_copy_plan(result, core_root=args["core_root"])  # type: ignore[arg-type]
    assert action["apply_status"] == "copied_verified"
    assert sha256_file(Path(action["target_path"])) == decision["source_sha256"]


def test_unlisted_identity_hold_blocks_apply(tmp_path: Path) -> None:
    args, _ = hold_fixture(tmp_path)
    result = build_intake(**args)  # type: ignore[arg-type]
    assert result.ready
    assert result.copy_actions[0]["planned_action"] == "hold_identity_review"

    with pytest.raises(ValueError, match="unresolved_identity_hold"):
        apply_copy_plan(result, core_root=args["core_root"])  # type: ignore[arg-type]


def test_identity_decision_rejects_target_outside_selected_versions(tmp_path: Path) -> None:
    args, decision = hold_fixture(tmp_path)
    decision["target_relative_path"] = "library/papers/other/versions/wrong.pdf"
    decision_file = tmp_path / "identity-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "paper-intake-identity-decisions/v1",
                "decisions": [decision],
            }
        ),
        encoding="utf-8",
    )

    result = build_intake(**args, identity_decisions_path=decision_file)  # type: ignore[arg-type]

    assert not result.ready
    assert any(
        error.startswith("identity_decision_target_outside_versions")
        for error in result.report["errors"]
    )


def test_identity_decision_cannot_approve_non_hold_match(tmp_path: Path) -> None:
    args, decision = hold_fixture(tmp_path)
    source = Path(str(args["metadata_path"]))
    metadata_row = json.loads(source.read_text(encoding="utf-8"))
    existing_pdf = (
        Path(str(args["core_root"]))
        / "VLM框架3DV压缩"
        / "library"
        / "papers"
        / "versioned-vlm-2026"
        / "paper.pdf"
    )
    metadata_row["source"]["path"] = str(existing_pdf)
    # Keep the file inside the Desktop scope while making bytes identical to the catalog PDF.
    desktop_source = (
        Path(str(args["desktop_root"]))
        / "Video"
        / "CVPR 2026 Versioned_ Efficient Video LLM.pdf"
    )
    desktop_source.write_bytes(existing_pdf.read_bytes())
    metadata_row["source"]["path"] = str(desktop_source)
    metadata_row["source"]["sha256"] = sha256_file(desktop_source)
    source.write_text(json.dumps(metadata_row) + "\n", encoding="utf-8")
    decision["source_sha256"] = sha256_file(desktop_source)
    decision_file = tmp_path / "identity-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "paper-intake-identity-decisions/v1",
                "decisions": [decision],
            }
        ),
        encoding="utf-8",
    )

    result = build_intake(**args, identity_decisions_path=decision_file)  # type: ignore[arg-type]

    assert not result.ready
    assert any(
        error.startswith("identity_decision_not_for_hold") for error in result.report["errors"]
    )


def test_explicit_routing_decision_resolves_needs_review(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    source = desktop / "其他" / "ICML 2026 Unclassified_ A Narrow Study.pdf"
    make_pdf(source, marker="routing")
    metadata = tmp_path / "metadata.jsonl"
    write_jsonl(
        metadata,
        [
            metadata_record(
                source,
                title="Unclassified: A Narrow Study",
                year=2026,
                venue="ICML",
            )
        ],
    )
    recovery = tmp_path / "recovery.json"
    write_empty_recovery(recovery)
    core = tmp_path / "core"
    catalog = core / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(catalog, [])
    decision_file = tmp_path / "routing-decisions.json"
    evidence = ["The paper studies a general-purpose agent observation representation."]
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "paper-intake-routing-decisions/v1",
                "decisions": [
                    {
                        "paper_id": "unclassified-2026",
                        "source_sha256": sha256_file(source),
                        "target_domain": "agent",
                        "evidence": evidence,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = build_intake(
        metadata_path=metadata,
        recovery_map_path=recovery,
        desktop_root=desktop,
        vlm_catalog_path=catalog,
        core_root=core,
        routing_decisions_path=decision_file,
        expected_count=1,
        validate_batch_shape=False,
    )

    assert result.ready
    assert result.report["routing_decisions"] == {
        "provided": 1,
        "applied": 1,
        "unresolved_needs_review": 0,
    }
    record = result.records[0]
    assert record["domain_slugs"] == ["agent"]
    assert record["routing_decision"]["evidence"] == evidence
    assert evidence[0] in record["decision_evidence"]
    assert result.copy_actions[0]["domain"] == "agent"
    apply_copy_plan(result, core_root=core)
    assert result.copy_actions[0]["apply_status"] == "copied_verified"


def test_unlisted_needs_review_blocks_apply(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    source = desktop / "其他" / "ICML 2026 Unclassified_ A Narrow Study.pdf"
    make_pdf(source, marker="routing")
    metadata = tmp_path / "metadata.jsonl"
    write_jsonl(
        metadata,
        [
            metadata_record(
                source,
                title="Unclassified: A Narrow Study",
                year=2026,
                venue="ICML",
            )
        ],
    )
    recovery = tmp_path / "recovery.json"
    write_empty_recovery(recovery)
    core = tmp_path / "core"
    catalog = core / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(catalog, [])
    result = build_intake(
        metadata_path=metadata,
        recovery_map_path=recovery,
        desktop_root=desktop,
        vlm_catalog_path=catalog,
        core_root=core,
        expected_count=1,
        validate_batch_shape=False,
    )
    assert result.ready
    with pytest.raises(ValueError, match="unresolved_routing_needs_review"):
        apply_copy_plan(result, core_root=core)


def test_routing_decision_rejects_unregistered_domain(tmp_path: Path) -> None:
    path = tmp_path / "routing.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "paper-intake-routing-decisions/v1",
                "decisions": [
                    {
                        "paper_id": "sample-2026",
                        "source_sha256": "a" * 64,
                        "target_domain": "invented-domain",
                        "evidence": ["Manual evidence."],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not an existing taxonomy slug"):
        load_routing_decisions(path, allowed_domains={"agent", "infra"})


def test_title_qa_and_manual_decisions_override_titles_but_not_low_confidence_authors(
    tmp_path: Path,
) -> None:
    desktop = tmp_path / "desktop"
    emcompress = desktop / "Video" / "CVPR 2026 EMCompress_ Wrong Acquisition Title.pdf"
    manual = desktop / "其他" / "ICML 2026 ManualMethod_ Truncated Titl.pdf"
    make_pdf(emcompress, marker="emcompress")
    make_pdf(manual, marker="manual")
    first_metadata = metadata_record(
        emcompress,
        title="EMCompress: Wrong Acquisition Title",
        year=2026,
        venue="CVPR",
    )
    second_metadata = metadata_record(
        manual,
        title="discarded",
        year=2026,
        venue="ICML",
    )
    second_metadata["facts"].pop("title")  # type: ignore[index]
    metadata = tmp_path / "metadata.jsonl"
    write_jsonl(metadata, [first_metadata, second_metadata])
    recovery = tmp_path / "recovery.json"
    write_empty_recovery(recovery)
    core = tmp_path / "core"
    catalog = core / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(catalog, [])
    first_key = f"desktop-{sha256_file(emcompress)[:24]}"
    second_key = f"desktop-{sha256_file(manual)[:24]}"
    qa = tmp_path / "title-qa.jsonl"
    qa.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                {
                    "record_key": first_key,
                    "status": "ok",
                    "source": {"sha256": sha256_file(emcompress)},
                    "title": {
                        "decision": "accepted_high_confidence",
                        "accepted": "Video-LLMs with Temporal Visual Screening",
                    },
                    "authors": {
                        "decision": "accepted_high_confidence",
                        "accepted": "Alice Example, Bob Example",
                    },
                    "conflicts": ["filename title conflicts with PDF title"],
                },
                {
                    "record_key": second_key,
                    "status": "ok",
                    "source": {"sha256": sha256_file(manual)},
                    "title": {"decision": "review_low_confidence", "accepted": None},
                    "authors": {
                        "decision": "review_low_confidence",
                        "accepted": "Wrong Low Confidence Author",
                    },
                    "conflicts": [],
                },
            ]
        ),
        encoding="utf-8",
    )
    decisions = tmp_path / "title-decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "schema_version": "paper-title-review-decisions/v1",
                "decisions": [
                    {
                        "record_key": second_key,
                        "title": "ManualMethod: Complete Official Title",
                        "evidence": ["Manually checked the PDF first-page title block."],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = build_intake(
        metadata_path=metadata,
        recovery_map_path=recovery,
        desktop_root=desktop,
        vlm_catalog_path=catalog,
        core_root=core,
        title_qa_results_path=qa,
        title_decisions_path=decisions,
        expected_count=2,
        validate_batch_shape=False,
    )

    assert result.ready
    by_id = {record["paper_id"]: record for record in result.records}
    emcompress_record = by_id["emcompress-2026"]
    assert emcompress_record["source_record_id"] == "emcompress-2026"
    assert emcompress_record["title"] == "Video-LLMs with Temporal Visual Screening"
    assert "EMCompress: Wrong Acquisition Title" in emcompress_record["title_aliases"]
    assert emcompress_record["authors"] == ["Alice Example", "Bob Example"]
    assert emcompress_record["code"]["url"] is None
    assert any(
        "do not infer or attach EMCompress code" in warning
        for warning in emcompress_record["warnings"]
    )

    manual_record = by_id["manualmethod-2026"]
    assert manual_record["title"] == "ManualMethod: Complete Official Title"
    assert manual_record["authors"] == ["Ada Example", "Bo Tester"]
    assert any("authors_not_overridden" in warning for warning in manual_record["warnings"])
    assert not any(
        "possibly truncated filename" in warning for warning in manual_record["warnings"]
    )
    assert result.report["title_qa"]["accepted_high_confidence_titles"] == 1
    assert result.report["title_qa"]["manual_title_decisions"] == 1


def test_author_resolution_preserves_structured_and_display_fallback_without_splitting() -> None:
    records = [
        {
            "paper_id": "structured-2026",
            "pdf": {"sha256": "a" * 64},
            "title_qa": {"record_key": "desktop-a"},
            "authors": [],
            "decision_evidence": [],
        },
        {
            "paper_id": "display-2026",
            "pdf": {"sha256": "b" * 64},
            "title_qa": {"record_key": "desktop-b"},
            "authors": [],
            "decision_evidence": [],
        },
    ]
    rows = [
        {
            "record_key": "desktop-a",
            "pdf_sha256": "a" * 64,
            "status": "verified_from_official_primary_source",
            "authors": ["Alice Example", "Bob Example"],
            "evidence": {"raw": "Alice Example; Bob Example", "source": "official"},
        },
        {
            "record_key": "desktop-b",
            "pdf_sha256": "b" * 64,
            "status": "verified_from_pdf_first_page_layout",
            "authors_display": "Name One Name Two Name Three",
            "evidence": {"raw": "Name One Name Two Name Three", "source": "layout"},
        },
    ]

    errors, summary = apply_author_resolution(records, rows)

    assert errors == []
    assert summary == {
        "resolution_records": 2,
        "matched": 2,
        "structured_authors": 1,
        "display_fallbacks": 1,
        "empty_or_invalid": 0,
        "final_empty": 0,
    }
    assert records[0]["authors"] == ["Alice Example", "Bob Example"]
    assert records[0]["display_fallback"] is False
    assert records[1]["authors_display"] == "Name One Name Two Name Three"
    assert records[1]["authors"] == ["Name One Name Two Name Three"]
    assert records[1]["display_fallback"] is True
    assert records[1]["author_resolution"]["raw"] == "Name One Name Two Name Three"


def test_duplicate_method_year_is_an_explicit_blocker(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    first = desktop / "Embodied" / "ICML 2026 SameMethod_ First Work.pdf"
    second = desktop / "其他" / "ICML 2026 SameMethod_ Different Work.pdf"
    make_pdf(first, marker="first")
    make_pdf(second, marker="second")
    metadata = tmp_path / "metadata.jsonl"
    write_jsonl(
        metadata,
        [
            metadata_record(first, title="SameMethod: First Work", year=2026, venue="ICML"),
            metadata_record(
                second,
                title="SameMethod: Different Work",
                year=2026,
                venue="ICML",
            ),
        ],
    )
    recovery = tmp_path / "recovery.json"
    write_empty_recovery(recovery)
    core = tmp_path / "core"
    catalog = core / "VLM框架3DV压缩" / "library" / "catalog" / "papers.v1.json"
    write_catalog(catalog, [])

    result = build_intake(
        metadata_path=metadata,
        recovery_map_path=recovery,
        desktop_root=desktop,
        vlm_catalog_path=catalog,
        core_root=core,
        expected_count=2,
        validate_batch_shape=False,
    )

    assert not result.ready
    assert any(
        error.startswith("paper_id_collision:samemethod-2026")
        for error in result.report["errors"]
    )


def test_apply_is_non_overwriting_hash_verified_and_idempotent(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    source = tmp_path / "source.pdf"
    target = core / "unit" / "paper.pdf"
    make_pdf(source, marker="one")
    digest = sha256_file(source)
    action = {
        "paper_id": "sample-2026",
        "source_group": "other-review",
        "domain": "infra",
        "source_path": str(source),
        "source_sha256": digest,
        "source_size": source.stat().st_size,
        "source_page_count": 1,
        "target_path": str(target),
        "planned_action": "copy_new",
        "reason": "test",
        "apply_status": "dry_run",
        "errors": [],
    }
    result = BuildResult(
        report={"status": "ready", "mode": "dry-run", "errors": []},
        records=[],
        copy_actions=[action],
    )

    apply_copy_plan(result, core_root=core)
    assert action["apply_status"] == "copied_verified"
    assert sha256_file(target) == digest
    apply_copy_plan(result, core_root=core)
    assert action["apply_status"] == "already_present_verified"

    target.unlink()
    make_pdf(target, marker="different")
    with pytest.raises(ValueError, match="target_conflict"):
        apply_copy_plan(result, core_root=core)


def test_outputs_include_json_csv_jsonl_and_markdown(tmp_path: Path) -> None:
    record = {
        "paper_id": "sample-2026",
        "source_record_id": "sample-2026",
        "canonical_paper_id": "sample-2026",
        "existing_vlm_id": None,
        "version_relation": None,
        "routing_decision": None,
        "source_group": "other-review",
        "disposition": "reclassify-retain",
        "title": "Sample",
        "authors": [],
        "year": 2026,
        "venue": "Test",
        "doi": None,
        "arxiv_id": None,
        "domain_slugs": ["needs-review"],
        "source_pdf_path": "source.pdf",
        "target_pdf_path": "target.pdf",
        "target_html_path": "paper.html",
        "pdf": {"sha256": "a" * 64, "size": 1, "page_count": 1},
        "routing": {"rule_ids": ["route-no-audited-rule"]},
        "existing_vlm_matches": [],
        "warnings": [],
    }
    action = {
        "paper_id": "sample-2026",
        "source_group": "other-review",
        "domain": "needs-review",
        "source_path": "source.pdf",
        "source_sha256": "a" * 64,
        "target_path": "target.pdf",
        "planned_action": "copy_new",
        "apply_status": "dry_run",
        "reason": "test",
        "errors": [],
    }
    result = BuildResult(
        report={
            "status": "ready",
            "mode": "dry-run",
            "record_count": 1,
            "expected_count": 1,
            "source_group_counts": {"other-review": 1},
            "errors": [],
        },
        records=[record],
        copy_actions=[action],
    )

    outputs = write_outputs(result, tmp_path / "out")

    assert set(outputs) == {
        "report",
        "paper_records_json",
        "paper_records_jsonl",
        "paper_records_csv",
        "copy_plan_json",
        "copy_plan_csv",
        "copy_plan_md",
    }
    assert all(Path(path).is_file() for path in outputs.values())
    assert "sample-2026" in Path(outputs["copy_plan_md"]).read_text(encoding="utf-8")
