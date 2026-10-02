from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfWriter

from tools.reclassified_paper_builder import builder as builder_module
from tools.reclassified_paper_builder.builder import (
    BuildError,
    BuildResult,
    _recover_incomplete_transaction,
    build_reclassified_papers,
)

DOMAINS = ("agent", "fine-tuning", "generation", "infra", "world-model")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pdf(path: Path, title: str) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as stream:
        writer.write(stream)
    return _sha256(path), path.stat().st_size


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _evidence(paper_id: str, sha256: str, *, limitations: bool = False) -> dict[str, object]:
    excerpt = "The method selects relevant evidence for the requested task."
    limit_excerpt = "A limitation remains and we leave its evaluation to future work."

    def item(value: str, number: int) -> dict[str, object]:
        return {
            "evidence_id": f"ev-{number:04d}",
            "section": "Limitations" if "limitation" in value.lower() else "Method",
            "page": 1,
            "excerpt": value,
            "excerpt_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "source_kind": "pdf_text",
            "locator": {"pdf_page": 1},
            "selection_reason": "fixture",
            "truncated": False,
        }

    return {
        "schema_version": "paper-analysis-evidence.v1",
        "tool_version": "fixture",
        "status": "ok",
        "paper": {"paper_id": paper_id},
        "policy": {"verbatim_evidence_only": True},
        "sources": {"pdf": {"sha256": sha256, "page_count": 1}},
        "evidence": {
            "abstract": [item(excerpt, 1)],
            "introduction_contributions": [item(excerpt, 2)],
            "method": [item(excerpt, 3)],
            "experiments_results": [item(excerpt, 4)],
            "limitations_conclusion": [item(limit_excerpt, 5)] if limitations else [],
            "code_urls": [],
        },
        "coverage": {},
        "missing_categories": [],
        "total_evidence_chars": len(excerpt),
        "warnings": [],
    }


def _setup(tmp_path: Path, *, official_url_only: bool = False) -> dict[str, Path]:
    core = tmp_path / "核心领域"
    target = core / "个人新增资料" / "2026-08-31-顶会压缩论文"
    evidence_dir = core / "inputs" / "evidence"
    records: list[dict[str, object]] = []
    audit_papers: list[dict[str, object]] = []
    repo = core / "code" / "official-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-f", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    for index, domain in enumerate(DOMAINS):
        paper_id = f"fixture-{domain}-{index}"
        title = f"Fixture {domain} Paper"
        pdf = target / domain / paper_id / "paper.pdf"
        sha256, size = _write_pdf(pdf, title)
        records.append(
            {
                "paper_id": paper_id,
                "source_record_id": paper_id,
                "canonical_paper_id": paper_id,
                "title": title,
                "authors": ["Fixture Author"],
                "year": 2026,
                "venue": "FixtureConf",
                "abstract": "This abstract is fixture evidence.",
                "disposition": "reclassify-retain",
                "domain_slugs": [domain],
                "target_pdf_path": str(pdf),
                "target_html_path": str(pdf.parent / "paper.html"),
                "target_metadata_path": str(pdf.parent / "metadata.json"),
                "pdf": {"path": "paper.pdf", "sha256": sha256, "size": size, "page_count": 1},
                "code": {
                    "status": "none_found",
                    "research_status": "none_found",
                    "evidence": ["fixture exact-title review"],
                },
            }
        )
        _write_json(evidence_dir / f"{paper_id}.evidence.json", _evidence(paper_id, sha256))
        if index == 0 and not official_url_only:
            audit_papers.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "status": "official_local",
                    "official_urls": ["https://github.com/example/fixture"],
                    "selected_local_archive": {
                        "path": str(repo),
                        "expected_commit": commit,
                        "url": "https://github.com/example/fixture",
                        "effective_license": "not-declared",
                    },
                    "html_api_mapping": {"html_code_status": "official_local"},
                }
            )
        elif index == 0 and official_url_only:
            audit_papers.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "status": "official_url_only",
                    "official_urls": ["https://github.com/example/fixture"],
                    "selected_local_archive": None,
                    "html_api_mapping": {"html_code_status": "official_url_only"},
                }
            )
        else:
            audit_papers.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "status": "not_provided",
                    "official_urls": [],
                    "no_code_review": {"paper_id": paper_id, "exact_title": title},
                    "html_api_mapping": {"html_code_status": "none_found"},
                }
            )
    records_path = core / "inputs" / "records.json"
    audit_path = core / "inputs" / "audit.json"
    _write_json(records_path, {"records": records})
    _write_json(audit_path, {"schema_version": "paper-code-audit/v1", "papers": audit_papers})
    return {
        "core": core,
        "target": target,
        "records": records_path,
        "audit": audit_path,
        "evidence": evidence_dir,
    }


def _build(fixture: dict[str, Path], staging: Path, *, apply: bool = False) -> BuildResult:
    with patch.object(builder_module, "DEFAULT_EXPECTED_COUNTS", {domain: 1 for domain in DOMAINS}):
        return build_reclassified_papers(
            paper_records=fixture["records"],
            evidence_dir=fixture["evidence"],
            code_audits=[fixture["audit"]],
            core_root=fixture["core"],
            target_root=fixture["target"],
            staging_root=staging,
            snapshot_date="2026-08-31",
            apply=apply,
        )


def test_dry_run_stages_complete_sidecars_without_touching_target(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    result = _build(fixture, fixture["core"] / "stage-dry")

    assert result.status == "ready"
    assert not result.applied
    for domain in DOMAINS:
        paper_dir = next(path for path in (fixture["target"] / domain).iterdir() if path.is_dir())
        assert [path.name for path in paper_dir.iterdir()] == ["paper.pdf"]
        staged = result.staging_root / "payload" / domain / paper_dir.name
        assert (staged / "metadata.json").is_file()
        assert (staged / "paper.html").is_file()
        assert (staged / "analysis-evidence.json").is_file()
        overview = (staged / "analysis.zh.md").read_text(encoding="utf-8")
        assert "局限与未来工作\n\n待核验。" in overview
    manifest = json.loads(
        (result.staging_root / "payload" / ".reclassified-paper-builder.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["no_guide"] is True
    assert manifest["record_count"] == 5
    assert all("guide" not in item.lower() for item in manifest["owned_paths"])


def test_apply_is_transactional_and_idempotent(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    first = _build(fixture, fixture["core"] / "stage-apply-1", apply=True)

    assert first.status == "applied"
    assert first.files_created == 5 * 4 + 5 * 3 + 1
    assert first.files_updated == 0
    for domain in DOMAINS:
        paper_dir = next(path for path in (fixture["target"] / domain).iterdir() if path.is_dir())
        assert (paper_dir / "paper.html").is_file()
        assert (fixture["target"] / domain / "catalog.json").is_file()

    second = _build(fixture, fixture["core"] / "stage-apply-2", apply=True)
    assert second.status == "applied"
    assert second.files_created == 0
    assert second.files_updated == 0
    assert second.files_unchanged == first.files_created


def test_official_url_only_stages_but_hard_blocks_apply(tmp_path: Path) -> None:
    fixture = _setup(tmp_path, official_url_only=True)
    staged = _build(fixture, fixture["core"] / "stage-blocked")
    assert staged.status == "staged_with_blockers"
    assert any(value.startswith("official_code_not_archived:") for value in staged.blockers)

    with pytest.raises(BuildError, match="apply 被硬门禁阻断"):
        _build(fixture, fixture["core"] / "stage-blocked-apply", apply=True)
    for domain in DOMAINS:
        paper_dir = next(path for path in (fixture["target"] / domain).iterdir() if path.is_dir())
        assert not (paper_dir / "paper.html").exists()


def test_missing_evidence_is_visible_and_blocks_apply(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    missing = next(fixture["evidence"].glob("fixture-agent-*.evidence.json"))
    missing.unlink()
    staged = _build(fixture, fixture["core"] / "stage-missing")
    assert "analysis_evidence_missing:fixture-agent-0" in staged.blockers
    with pytest.raises(BuildError, match="apply 被硬门禁阻断"):
        _build(fixture, fixture["core"] / "stage-missing-apply", apply=True)


def test_owned_hash_rejects_manual_edit(tmp_path: Path) -> None:
    fixture = _setup(tmp_path)
    _build(fixture, fixture["core"] / "stage-owned-1", apply=True)
    paper_dir = next(path for path in (fixture["target"] / "agent").iterdir() if path.is_dir())
    (paper_dir / "metadata.json").write_text("manual edit\n", encoding="utf-8")

    with pytest.raises(BuildError, match="拒绝覆盖"):
        _build(fixture, fixture["core"] / "stage-owned-2", apply=True)


def test_incomplete_transaction_is_recovered_from_durable_journal(tmp_path: Path) -> None:
    target = tmp_path / "target"
    generated = target / "agent" / "paper" / "metadata.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("new bytes\n", encoding="utf-8")
    transaction = target / ".reclassified-paper-builder.transaction-fixture"
    backup = transaction / "backups" / "agent" / "paper" / "metadata.json"
    backup.parent.mkdir(parents=True)
    backup.write_text("old bytes\n", encoding="utf-8")
    _write_json(
        target / ".reclassified-paper-builder.transaction.json",
        {
            "schema_version": "reclassified-paper-builder.transaction.v1",
            "status": "prepared",
            "transaction_dir": transaction.name,
            "operations": [
                {"path": "agent/paper/metadata.json", "existed": True},
                {"path": "agent/paper/paper.html", "existed": False},
            ],
        },
    )
    created = target / "agent" / "paper" / "paper.html"
    created.write_text("partial new page\n", encoding="utf-8")

    assert _recover_incomplete_transaction(target)
    assert generated.read_text(encoding="utf-8") == "old bytes\n"
    assert not created.exists()
    assert not transaction.exists()
    assert not (target / ".reclassified-paper-builder.transaction.json").exists()
