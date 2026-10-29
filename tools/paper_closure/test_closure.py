from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.paper_closure.closure import (
    EvidenceRecord,
    RelationEvidence,
    RepresentationEvidence,
    SourceRuntime,
    _catalog_record,
    _catalog_representation,
    _normalized_record_evidence,
    _raw_representation,
    audit_closure,
    load_census,
    load_document_evidence,
    load_retirement_status,
    load_sources_evidence,
    main,
    write_reports,
)


def _census_record(
    record_id: str,
    path: Path,
    sha256: str,
    *,
    title: str | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "root_id": "fixture-root",
        "root_path": str(path.parent),
        "relative_path": path.name,
        "absolute_path": str(path),
        "kind": "pdf",
        "root_in_core": False,
        "sha256": sha256,
        "pdf": {
            "magic_ok": True,
            "parse_status": "ok",
            "encrypted": False,
            "page_count": 12,
            "metadata_title": title,
        },
    }


def _census(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "paper-census/v1",
        "scope": {"complete": True},
        "records": records,
    }


def test_strong_match_keeps_hub_relation_separate_from_manifest_declaration(tmp_path: Path) -> None:
    core = tmp_path / "core"
    desktop = tmp_path / "desktop"
    html = core / "unit" / "paper.html"
    code = core / "unit" / "code"
    html.parent.mkdir(parents=True)
    html.write_text("<html>fixture</html>", encoding="utf-8")
    core_pdf = core / "unit" / "paper.pdf"
    core_pdf.write_bytes(b"%PDF-fixture")
    code.mkdir()
    pdf = desktop / "arxiv_2501.12345.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-fixture")
    sha = "a" * 64
    source = EvidenceRecord(
        record_key="source:fixture:paper-a",
        evidence_kind="source_manifest",
        asset_type="paper",
        title="A Strongly Identified Paper",
        year=2025,
        identifiers={"arxiv:2501.12345"},
        domains={"video-llm-compression"},
        representations=[
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256=sha),
            RepresentationEvidence("html", local_path=str(html)),
            RepresentationEvidence(
                "repository",
                local_path=str(code),
                url="https://github.com/example/paper-a",
            ),
        ],
        # Even a source-side confirmed declaration is not proof that the Hub
        # database contains the relation. Only Hub catalog evidence can confirm it.
        relations=[
            RelationEvidence(
                "implemented_by",
                target_source_key="fixture:paper-a-repository",
                confirmation_status="confirmed",
                confirmed=True,
            )
        ],
        source_id="fixture",
        source_record_id="paper-a",
        provenance_keys={"fixture:paper-a"},
    )
    hub = EvidenceRecord(
        record_key="hub:paper-asset",
        evidence_kind="hub_catalog",
        asset_type="paper",
        title="A Strongly Identified Paper",
        year=2025,
        identifiers={"arxiv:2501.12345"},
        asset_id="paper-asset",
        provenance_keys={"fixture:paper-a"},
        relations=[
            RelationEvidence(
                "implemented_by",
                target_asset_id="repo-asset",
                confirmation_status="confirmed",
                confirmed=True,
            )
        ],
    )
    repository = EvidenceRecord(
        record_key="hub:repo-asset",
        evidence_kind="hub_catalog",
        asset_type="repository",
        title="paper-a repository",
        year=None,
        asset_id="repo-asset",
        representations=[
            RepresentationEvidence(
                "repository",
                local_path=str(code),
                url="https://github.com/example/paper-a",
            )
        ],
    )
    declaration_only = audit_closure(
        _census([_census_record("pdf-a", pdf, sha, title="A Strongly Identified Paper")]),
        [source],
        core_root=core,
        batch_root=desktop,
        batch_expected_current=1,
        batch_expected_target=2,
    )["records"][0]
    assert declaration_only["hub_relation"]["implemented_by_status"] == "declared_unverified"
    assert "hub_implemented_by_unverified" in declaration_only["unresolved_flags"]

    report = audit_closure(
        _census([_census_record("pdf-a", pdf, sha, title="A Strongly Identified Paper")]),
        [source, hub, repository],
        core_root=core,
        batch_root=desktop,
        batch_expected_current=1,
        batch_expected_target=2,
    )
    record = report["records"][0]
    assert record["canonical_match"]["status"] == "matched_strong"
    assert record["hub_relation"]["implemented_by_status"] == "confirmed"
    assert record["html"]["status"] == "local"
    assert record["code"]["status"] == "local_and_url"
    assert record["pdf"]["equivalent_core_copy"] is True
    assert "pdf_not_in_core" not in record["unresolved_flags"]
    assert record["disposition"] == "closed"
    assert report["batch_check"]["current_count_matches"] is True
    assert report["batch_check"]["remaining_to_target"] == 1


def test_allowlisted_placeholder_url_does_not_require_a_local_clone(tmp_path: Path) -> None:
    core = tmp_path / "core"
    desktop = tmp_path / "desktop"
    paper_dir = core / "paper"
    paper_dir.mkdir(parents=True)
    core_pdf = paper_dir / "paper.pdf"
    core_pdf.write_bytes(b"%PDF-fixture")
    html = paper_dir / "paper.html"
    html.write_text("<html>fixture</html>", encoding="utf-8")
    desktop_pdf = desktop / "paper.pdf"
    desktop.mkdir()
    desktop_pdf.write_bytes(b"%PDF-fixture")
    sha = "d" * 64
    title = "Announced Code Placeholder"
    source = EvidenceRecord(
        record_key="source:fixture:announced",
        evidence_kind="source_manifest",
        asset_type="paper",
        title=title,
        year=2026,
        representations=[
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256=sha),
            RepresentationEvidence("html", local_path=str(html)),
            RepresentationEvidence(
                "repository",
                url="https://github.com/example/coming-soon",
            ),
        ],
        source_id="fixture",
        source_record_id="announced",
    )

    record = audit_closure(
        _census([_census_record("announced", desktop_pdf, sha, title=title)]),
        [source],
        core_root=core,
        batch_root=desktop,
        batch_expected_current=1,
        batch_expected_target=1,
        no_code_titles={title},
    )["records"][0]

    assert record["code"]["status"] == "allowlisted_no_code"
    assert record["code"]["urls"] == ["https://github.com/example/coming-soon"]
    assert "official_code_not_local" not in record["unresolved_flags"]
    assert record["disposition"] == "closed"


@pytest.mark.parametrize(
    ("raw_status", "expected_status"),
    [
        ("none_found", "explicit_no_code"),
        ("announced_pending", "announced_pending"),
        ("third_party_only", "third_party_only"),
    ],
)
def test_guide_code_status_is_an_explicit_closure_outcome(
    tmp_path: Path, raw_status: str, expected_status: str
) -> None:
    core = tmp_path / "core"
    paper_dir = core / "paper"
    paper_dir.mkdir(parents=True)
    core_pdf = paper_dir / "paper.pdf"
    core_pdf.write_bytes(b"%PDF-fixture")
    html = paper_dir / "paper.html"
    html.write_text("<html>fixture</html>", encoding="utf-8")
    desktop_pdf = tmp_path / "desktop" / "paper.pdf"
    desktop_pdf.parent.mkdir()
    desktop_pdf.write_bytes(b"%PDF-fixture")
    sha = "e" * 64
    source = EvidenceRecord(
        record_key="source:fixture:guide-status",
        evidence_kind="source_manifest",
        asset_type="paper",
        title="Guide Status Paper",
        year=2026,
        representations=[
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256=sha),
            RepresentationEvidence("html", local_path=str(html)),
            RepresentationEvidence(
                "guide",
                role="guide",
                metadata={"catalog_code_guide": {"status": raw_status}},
            ),
        ],
        source_id="fixture",
        source_record_id="guide-status",
    )
    record = audit_closure(
        _census([_census_record("guide-status", desktop_pdf, sha, title="Guide Status Paper")]),
        [source],
        core_root=core,
        batch_root=desktop_pdf.parent,
        batch_expected_current=1,
        batch_expected_target=1,
    )["records"][0]
    assert record["code"]["status"] == expected_status
    assert record["code"]["guide_status"] == expected_status
    assert "official_code_review_required" not in record["unresolved_flags"]
    assert record["disposition"] == "closed"


def test_source_adapter_asset_attributes_are_carried_into_explicit_code_closure(
    tmp_path: Path,
) -> None:
    asset = SimpleNamespace(
        asset_id="adapter-paper",
        asset_type="paper",
        title="Adapter Attribute Paper",
        year=2026,
        external_identifiers=[],
        domain_slugs=["fixture"],
        attributes={
            "code_status": "no_public_code_found",
            "no_code_reason": "No author or official repository was found.",
        },
    )
    normalized = SimpleNamespace(
        source_id="fixture",
        source_record_id="adapter-paper",
        asset=asset,
        representations=[],
        relations=[],
    )
    evidence = _normalized_record_evidence(
        normalized,
        SourceRuntime(source_id="fixture", root=tmp_path, asset_roots={}),
    )
    assert evidence.attributes["code_status"] == "no_public_code_found"
    assert evidence.attributes["no_code_reason"]

    core = tmp_path / "core"
    paper_dir = core / "paper"
    paper_dir.mkdir(parents=True)
    core_pdf = paper_dir / "paper.pdf"
    core_pdf.write_bytes(b"%PDF-fixture")
    (paper_dir / "paper.html").write_text("<html>fixture</html>", encoding="utf-8")
    evidence.representations.extend(
        [
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256="a" * 64),
            RepresentationEvidence("html", local_path=str(paper_dir / "paper.html")),
        ]
    )
    desktop_pdf = tmp_path / "desktop" / "paper.pdf"
    desktop_pdf.parent.mkdir()
    desktop_pdf.write_bytes(b"%PDF-fixture")
    result = audit_closure(
        _census([_census_record("adapter-paper", desktop_pdf, "a" * 64, title=asset.title)]),
        [evidence],
        core_root=core,
        batch_root=desktop_pdf.parent,
        batch_expected_current=1,
        batch_expected_target=1,
    )["records"][0]
    assert result["code"]["status"] == "explicit_no_code"
    assert result["code"]["explicit_no_code_reason"] == (
        "No author or official repository was found."
    )
    assert "official_code_review_required" not in result["unresolved_flags"]
    assert result["disposition"] == "closed"


def test_catalog_source_payload_code_attributes_are_supported_but_reason_alone_is_not(
    tmp_path: Path,
) -> None:
    catalog = _catalog_record(
        {
            "asset": {"asset_id": "payload-paper", "title": "Payload Attribute Paper"},
            "source_payload": {
                "attributes": {
                    "code_status": "no_public_code_found",
                    "no_code_reason": "Reviewed official locations.",
                }
            },
            "representations": [],
        },
        runtimes={},
        document_root=tmp_path,
    )
    assert catalog is not None
    assert catalog.attributes["code_status"] == "no_public_code_found"
    assert catalog.attributes["no_code_reason"] == "Reviewed official locations."

    core = tmp_path / "core"
    paper_dir = core / "paper"
    paper_dir.mkdir(parents=True)
    core_pdf = paper_dir / "paper.pdf"
    core_pdf.write_bytes(b"%PDF-fixture")
    html = paper_dir / "paper.html"
    html.write_text("<html>fixture</html>", encoding="utf-8")
    desktop_pdf = tmp_path / "desktop" / "paper.pdf"
    desktop_pdf.parent.mkdir()
    desktop_pdf.write_bytes(b"%PDF-fixture")
    catalog.representations.extend(
        [
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256="b" * 64),
            RepresentationEvidence("html", local_path=str(html)),
        ]
    )
    result = audit_closure(
        _census([_census_record("payload-paper", desktop_pdf, "b" * 64)]),
        [catalog],
        core_root=core,
        batch_root=desktop_pdf.parent,
        batch_expected_current=1,
        batch_expected_target=1,
    )["records"][0]
    assert result["code"]["status"] == "explicit_no_code"
    assert result["disposition"] == "closed"

    reason_only = EvidenceRecord(
        record_key="source:fixture:reason-only",
        evidence_kind="source_manifest",
        asset_type="paper",
        title="Reason Only Paper",
        year=2026,
        representations=[
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256="c" * 64),
            RepresentationEvidence("html", local_path=str(html)),
        ],
        attributes={"no_code_reason": "A reason without a status is not a decision."},
    )
    unresolved = audit_closure(
        _census([_census_record("reason-only", desktop_pdf, "c" * 64)]),
        [reason_only],
        core_root=core,
    )["records"][0]
    assert unresolved["code"]["status"] == "missing_or_unreviewed"
    assert "official_code_review_required" in unresolved["unresolved_flags"]


def test_downloaded_repository_evidence_takes_precedence_over_guide_status(tmp_path: Path) -> None:
    core = tmp_path / "core"
    paper_dir = core / "paper"
    paper_dir.mkdir(parents=True)
    core_pdf = paper_dir / "paper.pdf"
    core_pdf.write_bytes(b"%PDF-fixture")
    html = paper_dir / "paper.html"
    html.write_text("<html>fixture</html>", encoding="utf-8")
    repository = paper_dir / "repo"
    repository.mkdir()
    desktop_pdf = tmp_path / "desktop" / "paper.pdf"
    desktop_pdf.parent.mkdir()
    desktop_pdf.write_bytes(b"%PDF-fixture")
    sha = "f" * 64
    source = EvidenceRecord(
        record_key="source:fixture:downloaded-guide",
        evidence_kind="source_manifest",
        asset_type="paper",
        title="Downloaded Repository Paper",
        year=2026,
        representations=[
            RepresentationEvidence("pdf", local_path=str(core_pdf), sha256=sha),
            RepresentationEvidence("html", local_path=str(html)),
            RepresentationEvidence(
                "repository", local_path=str(repository), url="https://github.com/example/paper"
            ),
            RepresentationEvidence(
                "guide", role="guide", metadata={"status": "announced_pending"}
            ),
        ],
        source_id="fixture",
        source_record_id="downloaded-guide",
    )
    record = audit_closure(
        _census(
            [
                _census_record(
                    "downloaded-guide", desktop_pdf, sha, title="Downloaded Repository Paper"
                )
            ]
        ),
        [source],
        core_root=core,
        batch_root=desktop_pdf.parent,
        batch_expected_current=1,
        batch_expected_target=1,
    )["records"][0]
    assert record["code"]["status"] == "local_and_url"


def test_catalog_and_raw_representation_metadata_are_preserved(tmp_path: Path) -> None:
    catalog = _catalog_representation(
        {
            "representation_type": "guide",
            "role": "guide",
            "metadata": {"catalog_code_guide": {"status": "none_found"}},
        },
        {},
        tmp_path,
    )
    raw = _raw_representation(
        "guide",
        {
            "role": "guide",
            "path": "guide.md",
            "metadata": {"status": "third_party_only"},
        },
        document_root=tmp_path,
    )
    assert catalog is not None and catalog.metadata["catalog_code_guide"]["status"] == "none_found"
    assert raw is not None and raw.metadata["status"] == "third_party_only"


def test_catalog_code_guide_payload_outside_representations_is_preserved(tmp_path: Path) -> None:
    record = _catalog_record(
        {
            "asset": {"asset_id": "catalog-paper", "title": "Catalog Guide Paper"},
            "source_payload": {
                "catalog_code_guide": {"status": "announced_pending", "guide": "guide.md"}
            },
            "representations": [],
        },
        runtimes={},
        document_root=tmp_path,
    )
    assert record is not None
    assert record.representations[0].representation_type == "guide"
    assert record.representations[0].metadata["catalog_code_guide"]["status"] == "announced_pending"


def test_weak_title_is_candidate_only_and_never_auto_merges(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    pdf = core / "weak.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    candidate = EvidenceRecord(
        record_key="source:fixture:weak",
        evidence_kind="source_manifest",
        asset_type="paper",
        title="Token Compression for Long Video Understanding",
        year=2026,
        representations=[RepresentationEvidence("pdf", sha256="b" * 64)],
        source_id="fixture",
        source_record_id="weak",
    )
    report = audit_closure(
        _census(
            [
                _census_record(
                    "weak-pdf",
                    pdf,
                    "c" * 64,
                    title="Token Compression for Long Video Understanding",
                )
            ]
        ),
        [candidate],
        core_root=core,
    )
    record = report["records"][0]
    assert record["canonical_match"]["status"] == "weak_candidate_only"
    assert record["canonical_match"]["canonical_id"] is None
    assert record["weak_title_candidates"][0]["auto_merge"] is False
    assert record["disposition"] == "manual_identity_review"
    assert "weak_title_candidate_only" in record["unresolved_flags"]


def test_repository_pdf_is_visible_but_not_misreported_as_unregistered_paper(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    pdf = core / "shared-code" / "upstream" / "github" / "example" / "repo" / "figures" / "plot.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-fixture")

    census_record = _census_record("repo-plot", pdf, "f" * 64)
    census_record["relative_path"] = "shared-code/upstream/github/example/repo/figures/plot.pdf"
    report = audit_closure(
        _census([census_record]),
        [],
        core_root=core,
    )
    record = report["records"][0]
    assert record["classification"] == "repository_document"
    assert record["classification_reason"] == "undeclared_pdf_under_repository_tree:repo,upstream"
    assert record["disposition"] == "repository_document"
    assert record["unresolved_flags"] == []
    assert record["required_actions"] == []
    assert report["stats"]["formal_paper_count"] == 0
    assert report["stats"]["action_required_count"] == 0


def test_declared_pdf_and_acquisition_batch_override_nonpaper_path_markers(tmp_path: Path) -> None:
    core = tmp_path / "core"
    declared = core / "code" / "repo" / "paper.pdf"
    declared.parent.mkdir(parents=True)
    declared.write_bytes(b"%PDF-fixture")
    sha = "1" * 64
    source = EvidenceRecord(
        record_key="source:fixture:declared",
        evidence_kind="source_manifest",
        asset_type="paper",
        title="Declared Paper",
        year=2026,
        representations=[RepresentationEvidence("pdf", local_path=str(declared), sha256=sha)],
        source_id="fixture",
        source_record_id="declared",
    )
    direct = audit_closure(
        _census([_census_record("declared", declared, sha, title="Declared Paper")]),
        [source],
        core_root=core,
    )["records"][0]
    assert direct["classification"] == "formal_paper"
    assert direct["classification_reason"] == "exact_hub_declared_pdf_path"

    desktop = tmp_path / "desktop" / "code" / "figures" / "batch.pdf"
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"%PDF-fixture")
    batch = audit_closure(
        _census([_census_record("batch", desktop, "2" * 64)]),
        [],
        core_root=core,
        batch_root=tmp_path / "desktop",
        batch_expected_current=1,
        batch_expected_target=1,
    )["records"][0]
    assert batch["classification"] == "formal_paper"
    assert batch["classification_reason"] == "explicit_acquisition_batch"
    assert batch["disposition"] == "relocate_into_core"


def test_batch_check_supports_exact_336_and_future_376(tmp_path: Path) -> None:
    core = tmp_path / "core"
    batch = tmp_path / "desktop-batch"
    core.mkdir()
    batch.mkdir()

    def make(count: int) -> dict[str, object]:
        records = [
            _census_record(
                f"pdf-{index}",
                batch / f"paper-{index}.pdf",
                f"{index:064x}",
            )
            for index in range(count)
        ]
        return audit_closure(
            _census(records),
            [],
            core_root=core,
            batch_root=batch,
            batch_expected_current=336,
            batch_expected_target=376,
        )

    current = make(336)
    assert current["batch_check"]["current_count_matches"] is True
    assert current["batch_check"]["remaining_to_target"] == 40
    target = make(376)
    assert target["batch_check"]["target_reached"] is True
    assert target["batch_check"]["remaining_to_target"] == 0


def test_load_generic_source_and_open_catalog_then_write_all_formats(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    source_root = hub / "assets"
    source_root.mkdir(parents=True)
    (source_root / "paper.pdf").write_bytes(b"%PDF-fixture")
    (source_root / "paper.html").write_text("<html></html>", encoding="utf-8")
    (source_root / "code").mkdir()
    manifest = source_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "paper-a",
                        "title": "Fixture Paper",
                        "year": 2026,
                        "arxiv_id": "2601.00001",
                        "representations": {
                            "pdf": {"path": "paper.pdf", "sha256": "d" * 64},
                            "html": {"path": "paper.html"},
                            "code": {
                                "path": "code",
                                "url": "https://github.com/example/fixture",
                            },
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_dir = hub / "config"
    config_dir.mkdir()
    sources = config_dir / "sources.yaml"
    sources.write_text(
        """
sources:
  - id: fixture
    label: Fixture
    domain: fixture-domain
    adapter: generic_manifest
    root: assets
    manifest: manifest.json
    expected_records: 1
    read_only: true
    asset_roots:
      pdf: .
      html: .
      repository: .
    options:
      record_selector: items
      representations:
        - type: pdf
          root_key: pdf
          path_field: representations.pdf.path
          sha256_field: representations.pdf.sha256
        - type: html
          role: derived
          root_key: html
          path_field: representations.html.path
        - type: repository
          role: official
          root_key: repository
          path_field: representations.code.path
          url_field: representations.code.url
""".lstrip(),
        encoding="utf-8",
    )
    source_evidence, runtimes, errors = load_sources_evidence(sources, hub_root=hub)
    assert errors == []
    assert len(source_evidence) == 1
    catalog = hub / "catalog.jsonl"
    catalog.write_text(
        json.dumps(
            {
                "asset": {
                    "asset_id": "paper-asset",
                    "asset_type": "paper",
                    "title": "Fixture Paper",
                    "year": 2026,
                    "domains": ["fixture-domain"],
                    "external_identifiers": [{"scheme": "arxiv", "value": "2601.00001"}],
                },
                "representations": [],
                "relations": [
                    {
                        "relation_type": "implemented_by",
                        "object_asset_id": "repo-asset",
                        "confirmation_status": "confirmed",
                    }
                ],
                "provenance": {
                    "source_records": [{"source_alias": "fixture", "source_record_id": "paper-a"}]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    catalog_evidence, documents, catalog_errors = load_document_evidence(
        [catalog], runtimes=runtimes
    )
    assert catalog_errors == []
    assert documents[0]["record_count"] == 1
    census_path = hub / "census.json"
    census_path.write_text(
        json.dumps(
            _census(
                [
                    _census_record(
                        "paper-a-pdf",
                        source_root / "paper.pdf",
                        "d" * 64,
                        title="Fixture Paper",
                    )
                ]
            )
        ),
        encoding="utf-8",
    )
    census = load_census(census_path)
    report = audit_closure(
        census,
        [*source_evidence, *catalog_evidence],
        core_root=hub,
        source_errors=[*errors, *catalog_errors],
        input_documents=documents,
    )
    output = hub / "reports" / "closure"
    paths = write_reports(output, report)
    assert all(path.is_file() for path in paths)
    assert "weak_title_auto_merge" in paths[0].read_text(encoding="utf-8")
    assert "Fixture Paper" in paths[1].read_text(encoding="utf-8-sig")
    assert "Paper-level closure audit" in paths[2].read_text(encoding="utf-8")


def test_cli_batch_gate_returns_three_on_wrong_checkpoint(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    config = hub / "config"
    config.mkdir(parents=True)
    sources = config / "sources.yaml"
    sources.write_text("sources: []\n", encoding="utf-8")
    batch = tmp_path / "batch"
    batch.mkdir()
    census = hub / "census.json"
    census.write_text(
        json.dumps(_census([_census_record("one", batch / "one.pdf", "e" * 64)])),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "--census",
            str(census),
            "--sources-yaml",
            str(sources),
            "--hub-root",
            str(hub),
            "--core-root",
            str(hub),
            "--batch-root",
            str(batch),
            "--require-batch-count",
            "current",
            "--output-prefix",
            str(hub / "closure"),
        ]
    )
    assert exit_code == 3
    assert (hub / "closure.json").is_file()


def test_retirement_status_excludes_old_representation_from_serving_closure(tmp_path: Path) -> None:
    core = tmp_path / "core"
    desktop = tmp_path / "desktop"
    active_dir = core / "unit"
    active_dir.mkdir(parents=True)
    desktop.mkdir()
    active_pdf = active_dir / "paper.pdf"
    active_html = active_dir / "paper.html"
    active_pdf.write_bytes(b"%PDF-active")
    active_html.write_text("<html>active</html>", encoding="utf-8")
    old_pdf = desktop / "paper.pdf"
    old_pdf.write_bytes(b"%PDF-old")
    sha = "a" * 64
    title = "Retirement successor paper"
    retired = EvidenceRecord(
        record_key="source:old-source:paper",
        evidence_kind="source_manifest",
        asset_type="paper",
        title=title,
        year=2026,
        identifiers={"arxiv:2601.12345"},
        representations=[
            RepresentationEvidence("pdf", local_path=str(old_pdf), sha256=sha),
            RepresentationEvidence("html", local_path=str(tmp_path / "old.html")),
        ],
        source_id="old-source",
        source_record_id="paper",
    )
    active = EvidenceRecord(
        record_key="source:new-source:paper",
        evidence_kind="source_manifest",
        asset_type="paper",
        title=title,
        year=2026,
        identifiers={"arxiv:2601.12345"},
        representations=[
            RepresentationEvidence("pdf", local_path=str(active_pdf), sha256=sha),
            RepresentationEvidence("html", local_path=str(active_html)),
        ],
        source_id="new-source",
        source_record_id="paper",
    )
    status = tmp_path / "retirement.json"
    status.write_text(
        json.dumps(
            {"sources": [{"source_alias": "old-source", "lifecycle_status": "retired"}]}
        ),
        encoding="utf-8",
    )
    assert load_retirement_status(status) == ({"old-source"}, {"old-source"})
    record = audit_closure(
        _census([_census_record("old-pdf", old_pdf, sha, title=title)]),
        [retired, active],
        core_root=core,
        batch_root=desktop,
        batch_expected_current=1,
        batch_expected_target=1,
        no_code_titles={title},
        retirement_status_path=status,
    )["records"][0]
    assert record["retirement"]["historical_retired_count"] == 1
    assert record["retirement"]["historical_retired_aliases"] == ["old-source"]
    assert record["html"]["existing_paths"] == [str(active_html)]
    assert record["disposition"] == "closed"
