from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.existing_code_materializer.materializer import (
    AUDIT_SPECS,
    MaterializerError,
    _repository_target,
    build_plan,
    write_plan,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _official_rows() -> dict[str, dict[str, Any]]:
    return {
        "video": {
            "title": "Video Exact Paper",
            "status": "official_public",
            "canonical_repo_url": "https://github.com/Example/Shared/tree/main/src",
            "head": COMMIT_A,
            "license": "MIT",
            "release_scope": "The primary project links executable code.",
            "officiality_evidence": [
                {"url": "https://example.org/video", "finding": "Primary project page."}
            ],
        },
        "agent": {
            "paper_id": "legacy-agent-id",
            "exact_title": "Agent Exact Paper",
            "status": "official_public",
            "official_url": "https://github.com/Example/Shared/tree/main/configs",
            "commit": COMMIT_A,
            "license": "MIT",
            "evidence_urls": ["https://example.org/agent"],
            "evidence_reason": "The paper links the author repository.",
            "reviewed_at": "2026-08-31T10:00:00+08:00",
        },
        "fine-tuning": {
            "exact_title": "Fine Tuning Exact Paper",
            "status": "official_public",
            "repo_url": "https://gitlab.com/group/project/-/tree/main/pkg",
            "head": COMMIT_B,
            "license": "Apache-2.0",
            "evidence_urls": ["https://example.org/fine"],
            "evidence_reason": "Official publication points to GitLab.",
            "reviewed_at": "2026-08-31",
        },
        "generation": {
            "exact_title": "Generation Exact Paper",
            "status": "none_found",
            "license": None,
            "evidence_urls": ["https://example.org/generation"],
            "evidence_reason": "Primary paper and author pages have no code release.",
            "reviewed_at": "2026-08-31",
        },
        "infra": {
            "title": "Infra Exact Paper",
            "status": "official_public",
            "official_repo_url": "https://github.com/Infra/Repo",
            "pinned_head": COMMIT_C,
            "license": "BSD-3-Clause",
            "officiality_evidence": ["https://example.org/infra"],
            "notes": "Institutional README names the exact paper.",
        },
        "world-model": {
            "title": "World Model Exact Paper",
            "status": "third_party_only",
            "canonical_repo_url": None,
            "head": None,
            "license": None,
            "release_scope": "Only an unrelated reproduction was found.",
            "officiality_evidence": [
                {"url": "https://example.org/world", "finding": "Primary paper page."}
            ],
            "third_party_repositories": ["https://github.com/third/party"],
            "reviewed_at": "2026-08-31",
        },
        "vision-language-navigation": {
            "title": "VLN Exact Paper",
            "status": "announced_pending",
            "canonical_repo_url": None,
            "head": None,
            "license": None,
            "release_scope": "The official page promises a future release.",
            "officiality_evidence": [
                {"url": "https://example.org/vln", "finding": "Code is coming soon."}
            ],
            "reviewed_at": "2026-08-31",
        },
    }


def _catalog_row(domain: str, title: str, asset_id: str) -> dict[str, Any]:
    return {
        "export_schema": "research-asset-hub.open-catalog/v1",
        "asset": {
            "asset_id": asset_id,
            "asset_type": "paper",
            "title": title,
            "title_zh": f"中文 {title}",
            "authors": ["First Author", "Second Author"],
            "year": 2026,
            "venue": "TestConf",
            "source_urls": [f"https://example.org/catalog/{asset_id}"],
            "domains": [domain],
            "deleted_at": None,
            "external_identifiers": [
                {
                    "scheme": "arxiv",
                    "scope": "",
                    "value": f"2608.{asset_id[-4:]}",
                    "canonical": f"2608.{asset_id[-4:]}",
                    "is_identity": True,
                }
            ],
        },
        "representations": [],
    }


def _fixture(tmp_path: Path) -> dict[str, Any]:
    audit_dir = tmp_path / "audits"
    unit_dir = tmp_path / "unit-actions"
    core_root = tmp_path / "core"
    core_root.mkdir()
    rows = _official_rows()
    catalog_rows = []
    for index, spec in enumerate(AUDIT_SPECS, start=1):
        row = rows[spec.stem]
        title = str(row.get("exact_title") or row["title"])
        _write_jsonl(audit_dir / spec.audit_name, [row])
        log = (
            f"论文单元 1 个（domain={spec.domain}）\n\n"
            "❌ 1 处不匹配：\n"
            f"  - {title}: code 期望 available（或属于无代码白名单），实际 not_provided\n"
        )
        (unit_dir / spec.unit_action_name).parent.mkdir(parents=True, exist_ok=True)
        (unit_dir / spec.unit_action_name).write_text(log, encoding="utf-8")
        catalog_rows.append(_catalog_row(spec.domain, title, f"asset-{index:04d}"))
    catalog = tmp_path / "catalog.jsonl"
    _write_jsonl(catalog, catalog_rows)
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("{}\n", encoding="utf-8")
    return {
        "audit_dir": audit_dir,
        "unit_actions_dir": unit_dir,
        "catalog_path": catalog,
        "core_root": core_root,
        "current_allowlist_path": allowlist,
        "rows": rows,
        "catalog_rows": catalog_rows,
    }


def test_heterogeneous_inputs_build_direct_archiver_and_hub_drafts(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)

    result = build_plan(
        **{
            key: value
            for key, value in inputs.items()
            if key not in {"rows", "catalog_rows"}
        }
    )

    assert result.report["status"] == "passed"
    assert result.report["counts"] == {
        "audit_rows": 7,
        "paper_records": 7,
        "status": {
            "announced_pending": 1,
            "none_found": 1,
            "official_public": 4,
            "third_party_only": 1,
        },
        "official_public_papers": 4,
        "non_public_papers": 3,
        "unique_repositories": 3,
        "duplicate_remote_groups": 1,
        "allowlist_additions": 3,
        "allowlist_already_present": 0,
    }
    shared = next(
        item for item in result.approvals["repositories"] if item["url"].endswith("Example/Shared")
    )
    assert shared["commit"] == COMMIT_A
    assert shared["sparse_paths"] == ["configs", "src"]
    assert len(shared["papers"]) == 2
    assert shared["paper_evidence"][0]["original_official_url"].startswith("https://")
    assert result.manifests["video-llm-compression"]["items"][0]["representations"][
        "code"
    ] == {
        "path": "github/Example/Shared",
        "url": "https://github.com/Example/Shared",
        "commit": COMMIT_A,
        "license": "MIT",
        "status": "official_local",
    }
    assert result.report["source_expected_counts"]["world-model"] == 0
    assert result.allowlist_additions["generation"] == ("Generation Exact Paper",)

    outputs = write_plan(result, tmp_path / "out")
    saved = json.loads(Path(outputs["approvals"]).read_text(encoding="utf-8"))
    assert saved["schema_version"] == "paper-code-approvals/v1"
    source_plan = yaml.safe_load(Path(outputs["source_plan"]).read_text(encoding="utf-8"))
    assert len(source_plan["sources"]) == 4
    additions = yaml.safe_load(Path(outputs["allowlist_additions"]).read_text(encoding="utf-8"))
    assert additions["vision-language-navigation"]["no_code_titles"] == ["VLN Exact Paper"]


def test_repository_tree_subpaths_and_full_checkout_semantics() -> None:
    github = _repository_target("https://github.com/Owner/Repo/tree/release/src/module")
    gitlab = _repository_target("https://gitlab.com/group/sub/repo/-/tree/main/pkg/core")
    self_hosted = _repository_target("https://gitlab.mpi-sws.org/cld/ml/clockwork")

    assert github.url == "https://github.com/Owner/Repo"
    assert github.tree_ref == "release"
    assert github.sparse_path == "src/module"
    assert github.destination == "github/Owner/Repo"
    assert gitlab.url == "https://gitlab.com/group/sub/repo"
    assert gitlab.tree_ref == "main"
    assert gitlab.sparse_path == "pkg/core"
    assert gitlab.destination == "gitlab/group/sub/repo"
    assert self_hosted.url == "https://gitlab.mpi-sws.org/cld/ml/clockwork"
    assert self_hosted.destination == "gitlab.mpi-sws.org/cld/ml/clockwork"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://evilgithub.com/owner/repo",
        "https://user:pass@github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
        "https://github.com/owner/repo#fragment",
        "https://github.com/owner/repo/tree/main/../secret",
        "https://github.com/owner/repo/issues/1",
        "https://github.com/owner%2Frepo/project",
        "https://sub.gitlab.mpi-sws.org/cld/ml/clockwork",
        "https://gitlab.mpi-sws.org.evil/cld/ml/clockwork",
        "https://127.0.0.1/cld/ml/clockwork",
    ],
)
def test_malicious_or_non_repository_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        _repository_target(url)


def test_same_remote_conflicting_commits_fail_closed(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    rows = inputs["rows"]
    rows["agent"]["commit"] = COMMIT_B
    _write_jsonl(inputs["audit_dir"] / "agent.jsonl", [rows["agent"]])

    with pytest.raises(MaterializerError) as captured:
        build_plan(
            **{key: value for key, value in inputs.items() if key not in {"rows", "catalog_rows"}}
        )

    assert captured.value.code == "repository_commit_conflict"


def test_exact_title_catalog_ambiguity_requires_explicit_asset_resolution(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    duplicate = _catalog_row("agent", "Agent Exact Paper", "asset-ambiguous")
    catalog_rows = [*inputs["catalog_rows"], duplicate]
    _write_jsonl(inputs["catalog_path"], catalog_rows)

    with pytest.raises(MaterializerError) as captured:
        build_plan(
            **{key: value for key, value in inputs.items() if key not in {"rows", "catalog_rows"}}
        )
    assert captured.value.code == "catalog_exact_match_failed"

    resolutions = tmp_path / "resolutions.json"
    resolutions.write_text(
        json.dumps(
            {
                "resolutions": [
                    {
                        "domain": "agent",
                        "exact_title": "Agent Exact Paper",
                        "asset_id": "asset-0002",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    clean_inputs = {
        key: value
        for key, value in inputs.items()
        if key not in {"rows", "catalog_rows"}
    }
    clean_inputs["catalog_resolutions_path"] = resolutions
    result = build_plan(**clean_inputs)
    assert result.report["catalog"]["explicit_asset_resolutions"] == [
        {
            "domain": "agent",
            "exact_title": "Agent Exact Paper",
            "asset_id": "asset-0002",
        }
    ]


def test_audit_titles_must_cover_unit_action_failures_one_to_one(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    rows = inputs["rows"]
    rows["generation"]["exact_title"] = "Different Title"
    _write_jsonl(inputs["audit_dir"] / "generation.jsonl", [rows["generation"]])

    with pytest.raises(MaterializerError) as captured:
        build_plan(
            **{key: value for key, value in inputs.items() if key not in {"rows", "catalog_rows"}}
        )

    assert captured.value.code == "audit_failure_coverage_mismatch"
    assert captured.value.details["missing_titles"] == ["Generation Exact Paper"]
    assert captured.value.details["extra_titles"] == ["Different Title"]


def test_exact_seven_audit_files_are_required(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    (inputs["audit_dir"] / "extra.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(MaterializerError) as captured:
        build_plan(
            **{key: value for key, value in inputs.items() if key not in {"rows", "catalog_rows"}}
        )

    assert captured.value.code == "audit_file_set_mismatch"
    assert captured.value.details["extra_audit_files"] == ["extra.jsonl"]


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("status", "maybe_public", "invalid_status"),
        ("commit", "abc", "conflicting_aliases"),
        ("official_url", "https://github.com/other/repo", "conflicting_aliases"),
    ],
)
def test_conflicting_heterogeneous_aliases_and_status_fail(
    tmp_path: Path, field: str, value: str, error_code: str
) -> None:
    inputs = _fixture(tmp_path)
    row = inputs["rows"]["video"]
    row[field] = value
    _write_jsonl(inputs["audit_dir"] / "video.jsonl", [row])

    with pytest.raises(MaterializerError) as captured:
        build_plan(
            **{key: value for key, value in inputs.items() if key not in {"rows", "catalog_rows"}}
        )

    assert captured.value.code == error_code
