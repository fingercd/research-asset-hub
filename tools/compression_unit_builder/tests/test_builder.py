from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfWriter

from tools.compression_unit_builder.builder import (
    BuildError,
    _audit_v1_repositories,
    build_units,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PAPER_IDS = [
    "source-video-reuse-2025",
    "source-video-alt-2025",
    "new-video-2026",
    "embodied-compression-2026",
    "navigation-compression-2026",
]


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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _make_git_repository(path: Path, remote: str) -> str:
    path.mkdir(parents=True)
    _write(path / "README.md", "fixture repository\n")
    commands = [
        ["git", "init", "-q", str(path)],
        ["git", "-C", str(path), "config", "user.email", "fixture@example.invalid"],
        ["git", "-C", str(path), "config", "user.name", "Fixture"],
        ["git", "-C", str(path), "add", "README.md"],
        ["git", "-C", str(path), "commit", "-q", "-m", "fixture"],
        ["git", "-C", str(path), "remote", "add", "origin", remote],
    ]
    for command in commands:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    ).stdout.strip()


def _setup_fixture(tmp_path: Path) -> dict[str, Path]:
    core = tmp_path / "核心领域"
    core.mkdir(parents=True)
    vlm = core / "VLM框架3DV压缩"
    embodied = core / "具身智能压缩"
    navigation = core / "导航压缩"

    legacy_pdf = vlm / "library/papers/legacy-video-2025/paper.pdf"
    alt_pdf = vlm / "library/papers/legacy-video-2025/versions/camera-ready.pdf"
    video_pdf = vlm / "library/papers/new-video-2026/paper.pdf"
    embodied_pdf = embodied / "library/papers/embodied-compression-2026/paper.pdf"
    navigation_pdf = navigation / "library/papers/navigation-compression-2026/paper.pdf"
    values = {
        "LEGACY": _write_pdf(legacy_pdf, "Legacy Video Compression"),
        "ALT": _write_pdf(alt_pdf, "Legacy Video Compression Camera Ready"),
        "VIDEO": _write_pdf(video_pdf, "New Video Compression"),
        "EMBODIED": _write_pdf(embodied_pdf, "Embodied Compression Fixture"),
        "NAVIGATION": _write_pdf(navigation_pdf, "Navigation Compression Fixture"),
    }

    _write(vlm / "README.md", "# immutable v1 readme\n")
    _write(vlm / "index.html", "<!doctype html><title>immutable v1</title>\n")
    _write(vlm / "outputs/manifest.json", '{"schema_version":"videollm.library-manifest.v1"}\n')
    _write(vlm / "library/analysis/legacy-video-2025.md", "# immutable v1 analysis\n")
    _write(
        vlm / "library/site/papers/legacy-video-2025.html",
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>v1</title></html>\n',
    )
    (vlm / "library/catalog").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / "papers.v1.json", vlm / "library/catalog/papers.v1.json")
    shutil.copy2(FIXTURES / "repositories.v1.json", vlm / "library/catalog/repositories.v1.json")

    input_dir = core / "fixture-inputs"
    evidence_dir = input_dir / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence_template = (FIXTURES / "evidence.template.json").read_text(encoding="utf-8")
    for paper_id in PAPER_IDS:
        (evidence_dir / f"{paper_id}.evidence.json").write_text(
            evidence_template.replace("{PAPER_ID}", paper_id),
            encoding="utf-8",
            newline="\n",
        )
    records = (FIXTURES / "paper-records.template.json").read_text(encoding="utf-8")
    records = records.replace("{CORE_ROOT}", core.as_posix())
    for key, (sha256, size) in values.items():
        records = records.replace(f"{{{key}_SHA}}", sha256)
        records = records.replace(f"{{{key}_SIZE}}", str(size))
    records_path = input_dir / "paper-records.json"
    records_path.write_text(records, encoding="utf-8", newline="\n")
    code_path = input_dir / "code-audit.json"
    shutil.copy2(FIXTURES / "code-audit.json", code_path)
    expectations = input_dir / "expectations.json"
    shutil.copy2(FIXTURES / "expectations.json", expectations)
    return {
        "core": core,
        "vlm": vlm,
        "records": records_path,
        "evidence": evidence_dir,
        "code": code_path,
        "expectations": expectations,
    }


def _v1_hashes(vlm: Path) -> dict[str, str]:
    paths = [
        "README.md",
        "index.html",
        "outputs/manifest.json",
        "library/catalog/papers.v1.json",
        "library/catalog/repositories.v1.json",
        "library/analysis/legacy-video-2025.md",
        "library/site/papers/legacy-video-2025.html",
        "library/papers/legacy-video-2025/paper.pdf",
    ]
    return {path: _sha256(vlm / path) for path in paths}


def _build(
    fixture: dict[str, Path],
    staging: Path,
    *,
    apply: bool = False,
    version_recoveries: list[Path] | None = None,
):
    return build_units(
        paper_records=fixture["records"],
        evidence_dir=fixture["evidence"],
        code_audits=[fixture["code"]],
        version_recovery_reports=version_recoveries or [],
        core_root=fixture["core"],
        staging_root=staging,
        snapshot_date="2026-08-31",
        expectations_path=fixture["expectations"],
        apply=apply,
    )


def test_dry_run_builds_all_units_without_touching_targets(tmp_path: Path) -> None:
    fixture = _setup_fixture(tmp_path)
    before = _v1_hashes(fixture["vlm"])
    result = _build(fixture, fixture["core"] / "staging-dry-run")

    assert result.status == "ready"
    assert not result.applied
    assert _v1_hashes(fixture["vlm"]) == before
    by_domain = {unit.domain: unit for unit in result.units}
    assert by_domain["video-llm-compression"].counts == {
        "baseline_canonical": 1,
        "source_records": 3,
        "exact_reuse": 1,
        "alternate_versions": 1,
        "recovered_versions": 0,
        "version_pages": 1,
        "canonical_papers": 2,
        "generated_html": 3,
        "analysis_files": 3,
        "evidence_files": 3,
        "baseline_official_local": 0,
        "baseline_announced_pending": 0,
        "baseline_none_found": 1,
        "site_rows": 2,
    }
    assert by_domain["embodied-ai-compression"].counts["canonical_papers"] == 1
    assert by_domain["navigation-compression"].counts["canonical_papers"] == 1
    assert not (fixture["vlm"] / "README.v2.md").exists()
    for unit in result.units:
        manifest = json.loads(unit.manifest_path.read_text(encoding="utf-8"))
        assert manifest["no_guide"] is True
        assert manifest["blockers"] == []
        assert len(manifest["generated_html"]) == unit.counts["source_records"]
        assert all("guide" not in path.lower() for path in manifest["owned_paths"])


def test_apply_is_transactional_valid_and_idempotent(tmp_path: Path) -> None:
    fixture = _setup_fixture(tmp_path)
    before = _v1_hashes(fixture["vlm"])
    first = _build(fixture, fixture["core"] / "staging-apply-1", apply=True)

    assert first.status == "applied"
    assert first.files_created > 0
    assert first.files_updated == 0
    assert _v1_hashes(fixture["vlm"]) == before
    assert (fixture["vlm"] / "README.v2.md").is_file()
    assert (fixture["vlm"] / "outputs/manifest.v2.json").is_file()
    assert (
        fixture["vlm"] / "library/site/versions/legacy-video-2025/source-video-alt-2025.html"
    ).is_file()
    assert (fixture["core"] / "具身智能压缩/library/site/index.html").is_file()
    assert (fixture["core"] / "导航压缩/library/site/index.html").is_file()

    for unit in first.units:
        validator = unit.unit_root / (
            "scripts/validate_unit_v2.py"
            if unit.domain == "video-llm-compression"
            else "scripts/validate_unit.py"
        )
        completed = subprocess.run(
            [sys.executable, str(validator), "--unit-root", str(unit.unit_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    second = _build(fixture, fixture["core"] / "staging-apply-2", apply=True)
    assert second.status == "applied"
    assert second.files_created == 0
    assert second.files_updated == 0
    assert second.files_unchanged > 0
    assert _v1_hashes(fixture["vlm"]) == before


def test_missing_evidence_is_visible_and_blocks_apply(tmp_path: Path) -> None:
    fixture = _setup_fixture(tmp_path)
    (fixture["evidence"] / "new-video-2026.evidence.json").unlink()
    result = _build(fixture, fixture["core"] / "staging-blocked")
    assert result.status == "blocked"
    video = next(unit for unit in result.units if unit.domain == "video-llm-compression")
    assert "analysis_evidence_missing:new-video-2026" in video.blockers

    fixture2 = _setup_fixture(tmp_path / "apply-case")
    (fixture2["evidence"] / "new-video-2026.evidence.json").unlink()
    with pytest.raises(BuildError, match="apply 被硬门禁阻断"):
        _build(fixture2, fixture2["core"] / "staging-blocked-apply", apply=True)


def test_unarchived_official_code_blocks_apply(tmp_path: Path) -> None:
    fixture = _setup_fixture(tmp_path)
    audit = json.loads(fixture["code"].read_text(encoding="utf-8"))
    audit["papers"][2] = {
        "paper_id": "new-video-2026",
        "exact_title": "New Video Compression",
        "status": "official_public",
        "official_url": "https://github.com/example/new-video",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "evidence_reason": "Fixture primary source links the repository.",
    }
    fixture["code"].write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _build(fixture, fixture["core"] / "staging-code-blocked")
    video = next(unit for unit in result.units if unit.domain == "video-llm-compression")
    assert "official_code_not_archived:new-video-2026" in video.blockers
    assert result.status == "blocked"


def test_explicit_title_revision_keeps_two_pdfs_and_two_html_pages(tmp_path: Path) -> None:
    fixture = _setup_fixture(tmp_path)
    recovered_pdf = fixture["vlm"] / "library/papers/new-video-2026/versions/arxiv-2601.01234.pdf"
    recovered_sha, recovered_size = _write_pdf(recovered_pdf, "New Video Compression Current Title")
    report = {
        "schema_version": "paper-version-recovery/v1",
        "paper_id": "new-video-2026",
        "canonical_title": "New Video Compression",
        "alternate_title": "Old Temporal Screening Title",
        "identity_decision": "same_authors_method_benchmark_and_framework_title_revision",
        "arxiv_id": "2601.01234",
        "source_url": "https://arxiv.org/pdf/2601.01234",
        "local_path": str(recovered_pdf),
        "sha256": recovered_sha,
        "bytes": recovered_size,
        "pages": 1,
        "evidence": ["Fixture authors, method, benchmark and framework match across both PDFs."],
    }
    report_path = fixture["core"] / "fixture-inputs/version-recovery.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    expectations = json.loads(fixture["expectations"].read_text(encoding="utf-8"))
    video = expectations["domains"]["video-llm-compression"]
    video["recovered_versions"] = 1
    video["version_pages"] = 2
    video["generated_html"] = 4
    fixture["expectations"].write_text(
        json.dumps(expectations, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = _build(
        fixture,
        fixture["core"] / "staging-version-recovery",
        apply=True,
        version_recoveries=[report_path],
    )
    assert result.status == "applied"
    unit = next(item for item in result.units if item.domain == "video-llm-compression")
    catalog = json.loads(
        (unit.unit_root / "library/catalog/papers.v2.json").read_text(encoding="utf-8")
    )
    assert unit.counts["recovered_versions"] == 1
    assert unit.counts["version_pages"] == 2
    recovered = catalog["recovered_versions"][0]
    assert recovered["canonical_title"] == "New Video Compression"
    assert recovered["alternate_title"] == "Old Temporal Screening Title"
    assert recovered["code"]["status"] == "none_found"
    canonical = next(
        item
        for item in catalog["canonical_papers"]
        if item["canonical_paper_id"] == "new-video-2026"
    )
    assert canonical["primary_pdf_path"] == recovered["pdf"]["path"]
    assert canonical["primary_html_path"] == recovered["html_path"]
    assert canonical["aliases"] == ["Old Temporal Screening Title"]
    source = next(
        item for item in catalog["source_records"] if item["source_record_id"] == "new-video-2026"
    )
    assert source["version_role"] == "alternate_title_revision"
    assert source["html_path"].startswith("library/site/versions/new-video-2026/legacy/")
    assert (unit.unit_root / "library/site/papers/new-video-2026.html").is_file()
    assert (
        unit.unit_root / "library/site/versions/new-video-2026/legacy/new-video-2026.html"
    ).is_file()


def test_vlm_v1_code_corrections_require_clean_same_remote_replacement(
    tmp_path: Path,
) -> None:
    core = tmp_path / "核心领域"
    unit = core / "VLM框架3DV压缩"
    video_xl = unit / "library/code/upstream/video-xl-2025"
    videochat = unit / "library/code/upstream/videochat-flash-2026"
    xl_url = "https://github.com/VectorSpaceLab/Video-XL"
    chat_url = "https://github.com/OpenGVLab/VideoChat-Flash"
    xl_commit = _make_git_repository(video_xl, xl_url)
    chat_commit = _make_git_repository(videochat, chat_url)
    (video_xl / "README.md").unlink()
    (videochat / "README.md").unlink()

    shared_xl = core / "共享代码归档/github/vectorspacelab-video-xl"
    shared_xl_commit = _make_git_repository(shared_xl, xl_url)
    repositories = [
        {
            "paper_id": "liteframe-2026",
            "status": "valid",
            "url": "https://github.com/jjihwan/LiteFrame",
            "path": "library/code/upstream/liteframe-2026",
            "commit": "0" * 40,
        },
        {
            "paper_id": "video-xl-2025",
            "status": "valid",
            "url": xl_url,
            "path": "library/code/upstream/video-xl-2025",
            "commit": xl_commit,
        },
        {
            "paper_id": "videochat-flash-2026",
            "status": "valid",
            "url": chat_url,
            "path": "library/code/upstream/videochat-flash-2026",
            "commit": chat_commit,
        },
    ]
    audit = {
        "video-xl-2025": {
            "paper_id": "video-xl-2025",
            "status": "official_local",
            "official_url": xl_url,
            "local_path": str(shared_xl),
            "commit": shared_xl_commit,
            "license": "MIT",
            "evidence_reason": "Fixture clean shared archive with the same official remote.",
        }
    }
    corrected, counts, blockers, warnings = _audit_v1_repositories(
        repositories,
        code_audit=audit,
        unit_root=unit,
        core_root=core,
    )
    by_id = {item["paper_id"]: item for item in corrected}
    assert by_id["liteframe-2026"]["status"] == "announced_pending"
    assert by_id["liteframe-2026"]["path"] is None
    assert "vlm_v1_liteframe_corrected_to_announced_pending" in warnings
    assert by_id["video-xl-2025"]["status"] == "official_local"
    assert by_id["video-xl-2025"]["source"] == "shared-clean-fixed-commit-replacement"
    assert by_id["video-xl-2025"]["path"].startswith("../共享代码归档/")
    assert by_id["videochat-flash-2026"]["status"] == "blocked"
    assert any(
        item.startswith("vlm_v1_repository_unusable:videochat-flash-2026:") for item in blockers
    )
    assert counts == {
        "total": 3,
        "official_local": 1,
        "announced_pending": 1,
        "none_found": 0,
        "blocked": 1,
    }
