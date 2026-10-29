from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tools.paper_code_audit import audit


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _paper_record(
    *,
    paper_id: str = "paper-one",
    title: str = "Paper One",
    code: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "paper_id": paper_id,
        "title": title,
        "year": 2026,
        "pdf": {"path": f"papers/{paper_id}.pdf", "sha256": "a" * 64},
        "code": code or {"status": "unknown"},
    }


def _approval(
    *,
    papers: list[dict[str, str]] | None = None,
    url: str = "https://github.com/Example/Repo",
    commit: str = "b" * 40,
) -> dict[str, object]:
    return {
        "schema_version": audit.APPROVAL_SCHEMA_VERSION,
        "repositories": [
            {
                "url": url,
                "official": True,
                "commit": commit,
                "destination": "github/example-repo",
                "papers": papers or [{"paper_id": "paper-one", "exact_title": "Paper One"}],
                "sparse_paths": ["src"],
                "evidence": ["reviewed project page and paper statement"],
            }
        ],
        "no_code": [],
    }


def _ready_inspection(path: Path, core_root: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "inside_core": True,
        "commit": "b" * 40,
        "dirty": False,
        "remotes": [
            {
                "name": "origin",
                "url": "https://github.com/Example/Repo",
                "key": "github.com/example/repo",
            }
        ],
        "license": {"status": "MIT", "detected_labels": ["MIT"], "files": []},
        "submodules": [],
        "lfs": {
            "smudge_performed_by_tool": False,
            "files": [],
            "patterns": [],
            "command_available": True,
        },
        "inspection_errors": [],
    }


def test_canonical_link_strips_credentials_query_and_repository_subpaths() -> None:
    github = audit.canonical_link(
        "https://user:secret@github.com/Owner/Repo/tree/main?token=secret#readme"
    )
    gitlab = audit.canonical_link("git@gitlab.com:Group/Subgroup/Repo.git")
    project = audit.canonical_link("https://owner.github.io/project/?token=secret")

    assert github is not None
    assert github.url == "https://github.com/Owner/Repo"
    assert github.key == "github.com/owner/repo"
    assert gitlab is not None
    assert gitlab.url == "https://gitlab.com/Group/Subgroup/Repo"
    assert project is not None
    assert project.kind == "project_page"
    assert "secret" not in project.url
    assert audit.canonical_link("https://evilgithub.io/owner/repo") is None


def test_canonical_link_accepts_only_the_reviewed_self_hosted_gitlab() -> None:
    clockwork = audit.canonical_link("https://gitlab.mpi-sws.org/cld/ml/clockwork")

    assert clockwork is not None
    assert clockwork.kind == "repository"
    assert clockwork.url == "https://gitlab.mpi-sws.org/cld/ml/clockwork"
    assert clockwork.key == "gitlab.mpi-sws.org/cld/ml/clockwork"
    assert clockwork.clone_url == "https://gitlab.mpi-sws.org/cld/ml/clockwork.git"
    assert audit.canonical_link("https://sub.gitlab.mpi-sws.org/cld/ml/clockwork") is None
    assert audit.canonical_link("https://gitlab.mpi-sws.org.evil/cld/ml/clockwork") is None
    assert audit.canonical_link("https://10.0.0.1/cld/ml/clockwork") is None


def test_load_metadata_keeps_latest_jsonl_revision_and_reads_csv_identifiers(
    tmp_path: Path,
) -> None:
    metadata_jsonl = tmp_path / "metadata.jsonl"
    old = {
        "source": {"path": str(tmp_path / "paper.pdf"), "sha256": "a" * 64},
        "status": "error",
        "facts": {},
        "code_leads": [],
    }
    new = {
        "source": {"path": str(tmp_path / "paper.pdf"), "sha256": "a" * 64},
        "status": "ok",
        "facts": {
            "title": {"value": "Latest Title"},
            "doi": [{"value": "10.1000/example"}],
        },
        "code_leads": [],
    }
    metadata_jsonl.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    metadata_csv = tmp_path / "metadata.csv"
    with metadata_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "source_path",
                "sha256",
                "status",
                "title",
                "dois_json",
                "arxiv_ids_json",
                "code_leads_json",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "source_path": str(tmp_path / "other.pdf"),
                "sha256": "c" * 64,
                "status": "ok",
                "title": "Other",
                "dois_json": '["10.1000/other"]',
                "arxiv_ids_json": '["2601.00001"]',
                "code_leads_json": "[]",
            }
        )

    loaded = audit.load_metadata([metadata_jsonl, metadata_csv])

    assert len(loaded) == 2
    latest = next(item for item in loaded if item.source_path.endswith("paper.pdf"))
    other = next(item for item in loaded if item.source_path.endswith("other.pdf"))
    assert latest.status == "ok"
    assert latest.title == "Latest Title"
    assert latest.doi_values == ["10.1000/example"]
    assert other.doi_values == ["10.1000/other"]
    assert other.arxiv_values == ["2601.00001"]


def test_scan_repositories_indexes_sanitized_supported_remote(tmp_path: Path) -> None:
    repository = tmp_path / "code" / "repo"
    git_dir = repository / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = https://user:token@github.com/Owner/Repo.git?x=1\n',
        encoding="utf-8",
    )
    (repository / "node_modules" / "nested" / ".git").mkdir(parents=True)

    repositories, warnings = audit.scan_repositories([tmp_path])

    assert not warnings
    assert len(repositories) == 1
    assert repositories[0]["canonical_keys"] == ["github.com/owner/repo"]
    assert repositories[0]["remotes"][0]["url"] == "https://github.com/Owner/Repo"
    assert "token" not in json.dumps(repositories)


def test_candidate_pdf_lead_matches_local_remote_but_stays_unverified(
    tmp_path: Path,
) -> None:
    paper_path = tmp_path / "records.json"
    metadata_path = tmp_path / "metadata.jsonl"
    _write_json(paper_path, _paper_record())
    metadata_path.write_text(
        json.dumps(
            {
                "source": {
                    "path": str(tmp_path / "papers" / "paper-one.pdf"),
                    "sha256": "a" * 64,
                },
                "status": "ok",
                "facts": {"title": {"value": "Paper One"}},
                "code_leads": [
                    {
                        "type": "repository",
                        "url": "https://github.com/Example/Repo",
                        "confidence": 0.94,
                        "status": "candidate_unverified",
                        "evidence": [{"source": "annotation_uri", "location": "page:1"}],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    local_repository = tmp_path / "local-repo"
    repository_index = {"github.com/example/repo": [local_repository]}
    papers = audit.attach_metadata(
        audit.load_paper_records([paper_path], core_root=tmp_path),
        audit.load_metadata([metadata_path]),
    )

    result, queue = audit._paper_audit(
        papers[0],
        repository_index=repository_index,
        approvals=audit.ApprovalSet(),
        inspection_cache={},
        core_root=tmp_path,
    )

    assert result["status"] == "candidate_unverified"
    assert result["hard_block"] is True
    assert result["candidate_leads"][0]["existing_local_paths"] == [str(local_repository.resolve())]
    assert queue[0]["action"] == "verify_official_repository"


def test_structured_official_url_only_is_a_hard_block(tmp_path: Path) -> None:
    paper = audit.PaperEvidence(
        paper_id="paper-one",
        title="Paper One",
        year=2026,
        doi=None,
        arxiv_id=None,
        domains=["video-llm-compression"],
        record_file=None,
        code={
            "status": "official_url_only",
            "url": "https://github.com/Example/Repo",
        },
    )

    result, queue = audit._paper_audit(
        paper,
        repository_index={},
        approvals=audit.ApprovalSet(),
        inspection_cache={},
        core_root=tmp_path,
    )

    assert result["status"] == "official_url_only"
    assert result["hard_block"] is True
    assert result["html_api_mapping"]["check_unit_actions_acceptable"] is False
    assert result["html_api_mapping"]["hub_repository_availability"] is None
    assert queue[0]["priority"] == "blocking"


def test_alternate_pdf_representation_sha_attaches_metadata_without_synthetic_paper(
    tmp_path: Path,
) -> None:
    paper_path = tmp_path / "records.json"
    metadata_path = tmp_path / "metadata.jsonl"
    record = _paper_record()
    record["pdf"]["sha256"] = "b" * 64
    record["pdf_representations"] = [
        {"path": "legacy.pdf", "sha256": "a" * 64, "role": "alternate"}
    ]
    _write_json(paper_path, record)
    metadata_path.write_text(
        json.dumps(
            {
                "source": {"path": str(tmp_path / "legacy.pdf"), "sha256": "a" * 64},
                "status": "ok",
                "facts": {"title": {"value": "Paper One"}},
                "code_leads": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    papers = audit.attach_metadata(
        audit.load_paper_records([paper_path], core_root=tmp_path),
        audit.load_metadata([metadata_path]),
    )

    assert len(papers) == 1
    assert papers[0].synthetic is False
    assert len(papers[0].metadata) == 1


def test_approved_existing_repository_becomes_official_local_and_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records_path = _write_json(
        tmp_path / "records.json",
        [
            _paper_record(),
            _paper_record(paper_id="paper-two", title="Paper Two"),
        ],
    )
    approval_path = _write_json(
        tmp_path / "approved.json",
        _approval(
            papers=[
                {"paper_id": "paper-one", "exact_title": "Paper One"},
                {"paper_id": "paper-two", "exact_title": "Paper Two"},
            ]
        ),
    )
    repository = tmp_path / "repo"
    repository.mkdir()
    papers = audit.load_paper_records([records_path], core_root=tmp_path)
    approvals = audit.load_approvals(approval_path, papers)
    monkeypatch.setattr(
        audit,
        "inspect_repository",
        lambda path, core_root: _ready_inspection(path, core_root),
    )
    inspection_cache: dict[str, dict[str, object]] = {}

    first, _ = audit._paper_audit(
        papers[0],
        repository_index={"github.com/example/repo": [repository]},
        approvals=approvals,
        inspection_cache=inspection_cache,
        core_root=tmp_path,
    )
    second, _ = audit._paper_audit(
        papers[1],
        repository_index={"github.com/example/repo": [repository]},
        approvals=approvals,
        inspection_cache=inspection_cache,
        core_root=tmp_path,
    )
    plan = audit.build_clone_plan(
        approvals,
        repository_index={"github.com/example/repo": [repository]},
        archive_root=tmp_path,
    )

    assert first["status"] == second["status"] == "official_local"
    assert first["html_api_mapping"]["api_code_action_expected"] == "available"
    assert first["selected_local_archive"]["evidence"]["dirty"] is False
    assert len(plan) == 1
    assert plan[0]["status"] == "reuse_existing"
    assert plan[0]["paper_ids"] == ["paper-one", "paper-two"]


def test_reviewed_no_code_becomes_not_provided_allowlist_candidate(tmp_path: Path) -> None:
    records_path = _write_json(tmp_path / "records.json", _paper_record())
    approval_path = _write_json(
        tmp_path / "approved.json",
        {
            "schema_version": audit.APPROVAL_SCHEMA_VERSION,
            "repositories": [],
            "no_code": [
                {
                    "paper_id": "paper-one",
                    "exact_title": "Paper One",
                    "verified_no_public_code": True,
                    "reviewed_at": "2026-08-31",
                    "evidence_urls": ["https://example.org/search?token=must-not-appear"],
                }
            ],
        },
    )
    papers = audit.load_paper_records([records_path], core_root=tmp_path)
    approvals = audit.load_approvals(approval_path, papers)

    result, queue = audit._paper_audit(
        papers[0],
        repository_index={},
        approvals=approvals,
        inspection_cache={},
        core_root=tmp_path,
    )

    assert result["status"] == "not_provided"
    assert result["hard_block"] is False
    assert result["html_api_mapping"]["api_code_action_expected"] == "not_provided"
    assert result["html_api_mapping"]["allowlist_exact_title_candidate"] == "Paper One"
    assert result["no_code_review"]["evidence_urls"] == ("https://example.org/search",)
    assert not queue


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["repositories"][0].update(commit="abc"), "exact 40-hex"),
        (
            lambda value: value["repositories"][0].update(
                url="https://user:secret@github.com/Example/Repo?token=x"
            ),
            "credentials/query/fragment",
        ),
        (
            lambda value: value["repositories"][0]["papers"][0].update(exact_title="Wrong title"),
            "does not match",
        ),
    ],
)
def test_repository_approval_fails_closed(tmp_path: Path, mutator: object, message: str) -> None:
    records_path = _write_json(tmp_path / "records.json", _paper_record())
    papers = audit.load_paper_records([records_path], core_root=tmp_path)
    value = _approval()
    mutator(value)  # type: ignore[operator]
    approval_path = _write_json(tmp_path / "approval.json", value)

    with pytest.raises(ValueError, match=message):
        audit.load_approvals(approval_path, papers)


def test_repository_approval_accepts_reviewed_self_hosted_gitlab_https(
    tmp_path: Path,
) -> None:
    records_path = _write_json(tmp_path / "records.json", _paper_record())
    papers = audit.load_paper_records([records_path], core_root=tmp_path)
    approval_path = _write_json(
        tmp_path / "approval.json",
        _approval(url="https://gitlab.mpi-sws.org/cld/ml/clockwork"),
    )

    approvals = audit.load_approvals(approval_path, papers)

    assert list(approvals.repositories) == ["gitlab.mpi-sws.org/cld/ml/clockwork"]


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.mpi-sws.org/cld/ml/clockwork",
        "https://user:pass@gitlab.mpi-sws.org/cld/ml/clockwork",
        "https://gitlab.mpi-sws.org:443/cld/ml/clockwork",
        "https://gitlab.mpi-sws.org/cld/ml/clockwork?token=secret",
        "https://gitlab.mpi-sws.org/cld/ml/clockwork#readme",
        "https://sub.gitlab.mpi-sws.org/cld/ml/clockwork",
        "https://gitlab.mpi-sws.org.evil/cld/ml/clockwork",
        "https://127.0.0.1/cld/ml/clockwork",
    ],
)
def test_repository_approval_rejects_unsafe_self_hosted_gitlab_variants(
    tmp_path: Path, url: str
) -> None:
    records_path = _write_json(tmp_path / "records.json", _paper_record())
    papers = audit.load_paper_records([records_path], core_root=tmp_path)
    approval_path = _write_json(tmp_path / "approval.json", _approval(url=url))

    with pytest.raises(ValueError):
        audit.load_approvals(approval_path, papers)


def test_clone_plan_is_pinned_partial_lfs_safe_and_does_not_overwrite(tmp_path: Path) -> None:
    records_path = _write_json(tmp_path / "records.json", _paper_record())
    approval_path = _write_json(tmp_path / "approval.json", _approval())
    papers = audit.load_paper_records([records_path], core_root=tmp_path)
    approvals = audit.load_approvals(approval_path, papers)

    plan = audit.build_clone_plan(
        approvals,
        repository_index={},
        archive_root=tmp_path,
    )

    assert len(plan) == 1
    assert plan[0]["environment"]["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert "--filter=blob:none" in plan[0]["commands"][0]
    assert plan[0]["commands"][1][-1] == "b" * 40
    destination = Path(plan[0]["destination"])
    destination.mkdir(parents=True)
    blocked = audit._execute_clone(
        next(iter(approvals.repositories.values())), archive_root=tmp_path
    )
    assert blocked["status"] == "failed"
    assert blocked["reason"] == "destination_exists"


def test_weight_and_dataset_tree_paths_are_blocked_with_sparse_scope() -> None:
    lines = [
        "100644 blob aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 10\tREADME.md",
        "100644 blob bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 10\tsrc/main.py",
        "100644 blob cccccccccccccccccccccccccccccccccccccccc 10\tsrc/weights/model.pth",
        "100644 blob dddddddddddddddddddddddddddddddddddddddd 10\tdocs/demo.npy",
    ]

    assert audit._blocked_tree_paths(lines, sparse_paths=["src"]) == ["src/weights/model.pth"]
    assert audit._blocked_tree_paths(lines, sparse_paths=[]) == [
        "docs/demo.npy",
        "src/weights/model.pth",
    ]
    assert audit._blocked_tree_paths(
        ["README.md", "src/main.py", "src/weights/model.pth"], sparse_paths=["src"]
    ) == ["src/weights/model.pth"]


def test_write_reports_emits_json_csv_network_queue_and_clone_plan(tmp_path: Path) -> None:
    report = {
        "summary": {"hard_blockers": 1},
        "papers": [
            {
                "paper_id": "p",
                "title": "Paper",
                "year": 2026,
                "domains": [],
                "status": "unknown",
                "hard_block": True,
                "official_urls": [],
                "local_matches": [],
                "candidate_leads": [],
                "html_api_mapping": {
                    "api_code_action_expected": None,
                    "allowlist_exact_title_candidate": None,
                },
                "issues": [],
            }
        ],
        "network_queue": [
            {
                "paper_id": "p",
                "action": "search_official_code",
                "url": None,
                "priority": "review",
                "reason": "test",
            }
        ],
        "clone_plan": [],
    }

    paths = audit.write_reports(tmp_path / "report", report)

    assert all(path.is_file() for path in paths)
    assert json.loads(paths[0].read_text(encoding="utf-8"))["papers"][0]["paper_id"] == "p"
    assert "search_official_code" in paths[2].read_text(encoding="utf-8")
