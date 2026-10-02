from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.paper_code_audit import compile_research as compiler


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _research_row(
    paper_id: str,
    title: str,
    *,
    status: str,
    url: str | None = None,
    commit: str | None = None,
) -> dict[str, object]:
    return {
        "paper_id": paper_id,
        "exact_title": title,
        "status": status,
        "official_url": url,
        "commit": commit,
        "license": "MIT" if status == "official_public" else None,
        "evidence_urls": [url or f"https://example.org/papers/{paper_id}?q=public"],
        "evidence_reason": f"reviewed evidence for {paper_id}",
        "reviewed_at": "2026-08-31T00:00:00+08:00",
    }


def _paper_record(paper_id: str, title: str) -> dict[str, object]:
    return {
        "paper_id": paper_id,
        "title": title,
        "year": 2026,
        "pdf": {"path": "paper.pdf", "sha256": paper_id[0] * 64},
        "code": {"status": "unknown"},
    }


def _small_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], Path]:
    monkeypatch.setattr(compiler, "EXPECTED_BATCHES", 2)
    monkeypatch.setattr(compiler, "EXPECTED_TOTAL", 4)
    monkeypatch.setattr(compiler, "EXPECTED_PUBLIC", 2)
    monkeypatch.setattr(compiler, "EXPECTED_NON_PUBLIC", 2)
    commit = "a" * 40
    first = _write_jsonl(
        tmp_path / "batch-00-01.jsonl",
        [
            _research_row(
                "alpha",
                "Alpha Paper",
                status="official_public",
                url="https://github.com/Org/Repo/tree/main/src/alpha",
                commit=commit,
            ),
            _research_row("gamma", "Gamma Paper", status="none_found"),
        ],
    )
    second = _write_jsonl(
        tmp_path / "batch-02-03.jsonl",
        [
            _research_row(
                "beta",
                "Beta Paper",
                status="official_public",
                url="https://github.com/org/repo/tree/main/src/beta",
                commit=commit,
            ),
            _research_row("delta", "Delta Paper", status="third_party_only"),
        ],
    )
    records = _write_jsonl(
        tmp_path / "paper-records.jsonl",
        [
            _paper_record("alpha", "Alpha Paper"),
            _paper_record("beta", "Beta Paper"),
            _paper_record("gamma", "Gamma Paper"),
            _paper_record("delta", "Delta Paper"),
        ],
    )
    return [first, second], records


def test_repository_target_normalizes_tree_url_and_destination() -> None:
    github = compiler._repository_target("https://github.com/Microsoft/MInference/tree/main/LeanK")
    gitlab = compiler._repository_target("https://gitlab.com/Group/Sub/Repo/-/tree/main/src/module")

    assert github.url == "https://github.com/Microsoft/MInference"
    assert github.destination == "github/microsoft/minference"
    assert github.tree_ref == "main"
    assert github.sparse_path == "LeanK"
    assert gitlab.url == "https://gitlab.com/Group/Sub/Repo"
    assert gitlab.destination == "gitlab/group/sub/repo"
    assert gitlab.sparse_path == "src/module"


def test_repository_target_accepts_reviewed_self_hosted_gitlab() -> None:
    target = compiler._repository_target(
        "https://gitlab.mpi-sws.org/cld/ml/clockwork/-/tree/master/src/runtime"
    )

    assert target.url == "https://gitlab.mpi-sws.org/cld/ml/clockwork"
    assert target.clone_url == "https://gitlab.mpi-sws.org/cld/ml/clockwork.git"
    assert target.destination == "gitlab.mpi-sws.org/cld/ml/clockwork"
    assert target.tree_ref == "master"
    assert target.sparse_path == "src/runtime"


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
def test_repository_target_rejects_unsafe_self_hosted_gitlab_variants(url: str) -> None:
    with pytest.raises(ValueError):
        compiler._repository_target(url)


def test_compile_deduplicates_repository_and_preserves_non_public_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches, records = _small_fixture(tmp_path, monkeypatch)

    document, audit = compiler.compile_research(
        batch_paths=batches,
        paper_record_path=records,
    )

    assert len(document["repositories"]) == 1
    repository = document["repositories"][0]
    assert repository["commit"] == "a" * 40
    assert repository["sparse_paths"] == ["src/alpha", "src/beta"]
    assert [item["paper_id"] for item in repository["papers"]] == ["alpha", "beta"]
    assert len(document["no_code"]) == 2
    assert {item["research_status"] for item in document["no_code"]} == {
        "none_found",
        "third_party_only",
    }
    assert audit["unique_repositories"] == 1
    assert len(audit["shared_repositories"]) == 1
    assert audit["clone_plan"][0]["action"] == "approved_not_applied"


def test_same_repository_commit_conflict_is_a_hard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches, records = _small_fixture(tmp_path, monkeypatch)
    rows = [json.loads(line) for line in batches[1].read_text(encoding="utf-8").splitlines()]
    rows[0]["commit"] = "b" * 40
    _write_jsonl(batches[1], rows)

    with pytest.raises(ValueError, match="conflicting commits"):
        compiler.compile_research(batch_paths=batches, paper_record_path=records)


def test_duplicate_paper_id_and_sensitive_evidence_query_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches, records = _small_fixture(tmp_path, monkeypatch)
    rows = [json.loads(line) for line in batches[1].read_text(encoding="utf-8").splitlines()]
    rows[0]["paper_id"] = "alpha"
    _write_jsonl(batches[1], rows)
    with pytest.raises(ValueError, match="duplicate research paper_id"):
        compiler.compile_research(batch_paths=batches, paper_record_path=records)

    batches, records = _small_fixture(tmp_path, monkeypatch)
    rows = [json.loads(line) for line in batches[0].read_text(encoding="utf-8").splitlines()]
    rows[0]["evidence_urls"] = ["https://example.org/evidence?access_token=secret"]
    _write_jsonl(batches[0], rows)
    with pytest.raises(ValueError, match="sensitive query key"):
        compiler.compile_research(batch_paths=batches, paper_record_path=records)


def test_agent_reviewed_title_override_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches, records = _small_fixture(tmp_path, monkeypatch)
    paper_rows = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
    paper_rows[0]["title"] = "Stale and unrelated extracted title"
    _write_jsonl(records, paper_rows)

    document, _ = compiler.compile_research(
        batch_paths=batches,
        paper_record_path=records,
    )

    binding = document["repositories"][0]["papers"][0]
    assert binding["paper_id"] == "alpha"
    assert binding["exact_title"] == "Alpha Paper"
    assert binding["paper_record_title"] == "Stale and unrelated extracted title"
    assert binding["title_match"] == "agent_reviewed_exact_title_override"


def test_write_compiled_outputs_revalidates_runtime_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches, records = _small_fixture(tmp_path, monkeypatch)
    document, audit = compiler.compile_research(
        batch_paths=batches,
        paper_record_path=records,
    )
    summary = compiler.render_summary(audit, document)
    output = tmp_path / "approvals.json"
    summary_path = tmp_path / "summary.md"

    compiler.write_compiled_outputs(
        document=document,
        summary=summary,
        approvals_output=output,
        summary_output=summary_path,
        paper_record_path=records,
    )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "paper-code-approvals/v1"
    assert saved["audit"]["network_or_clone_performed"] is False
    assert "预计 clone 计划（未执行）" in summary_path.read_text(encoding="utf-8")
