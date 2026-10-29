from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.paper_code_audit import bind_current_records as binder


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )
    return path


def _records(path: Path) -> Path:
    return _write_jsonl(
        path,
        [
            {
                "paper_id": "paper-a",
                "title": "Current Exact Title A",
                "year": 2026,
                "pdf": {"path": "a.pdf", "sha256": "a" * 64},
                "code": {"status": "unknown"},
            },
            {
                "paper_id": "paper-b",
                "title": "Current Exact Title B",
                "year": 2026,
                "pdf": {"path": "b.pdf", "sha256": "b" * 64},
                "code": {"status": "unknown"},
            },
        ],
    )


def _approvals() -> dict[str, object]:
    return {
        "schema_version": "paper-code-approvals/v1",
        "repositories": [
            {
                "url": "https://github.com/example/repo",
                "official": True,
                "commit": "c" * 40,
                "destination": "github/example/repo",
                "papers": [
                    {
                        "paper_id": "paper-a",
                        "exact_title": "Agent Reviewed Research Title A",
                        "paper_record_title": "Old Extracted Title A",
                        "title_match": "agent_reviewed_exact_title_override",
                    }
                ],
                "sparse_paths": [],
                "evidence": ["reviewed repository evidence"],
                "licenses": ["MIT"],
                "paper_evidence": [
                    {
                        "paper_id": "paper-a",
                        "research_status": "official_public",
                        "reviewed_at": "2026-08-31",
                        "license": "MIT",
                        "source_batch": "batch.jsonl",
                        "source_line": 1,
                        "original_official_url": "https://github.com/example/repo",
                        "tree_ref": None,
                        "sparse_path": None,
                        "evidence_urls": ["https://github.com/example/repo"],
                        "evidence_reason": "author repository verified",
                    }
                ],
            }
        ],
        "no_code": [
            {
                "paper_id": "paper-b",
                "exact_title": "Agent Reviewed Research Title B",
                "paper_record_title": "Old Extracted Title B",
                "title_match": "agent_reviewed_exact_title_override",
                "verified_no_public_code": True,
                "reviewed_at": "2026-08-31",
                "research_status": "none_found",
                "announced_url": None,
                "announced_commit": None,
                "license": None,
                "evidence_reason": "exact-title search found no official code",
                "source_batch": "batch.jsonl",
                "source_line": 2,
                "evidence_urls": ["https://example.org/search?q=paper-b"],
            }
        ],
        "audit": {"network_or_clone_performed": False},
    }


def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, object]]:
    monkeypatch.setattr(binder, "EXPECTED_TOTAL", 2)
    source = _approvals()
    approvals_path = _write_json(tmp_path / "research-approvals.json", source)
    records_path = _records(tmp_path / "paper-records.jsonl")
    return approvals_path, records_path, source


def test_bind_uses_current_exact_title_and_preserves_research_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approvals_path, records_path, source = _fixture(tmp_path, monkeypatch)
    original = copy.deepcopy(source)

    document = binder.bind_current_records(
        research_approvals_path=approvals_path,
        paper_record_path=records_path,
    )

    repository = document["repositories"][0]
    binding = repository["papers"][0]
    assert binding["exact_title"] == "Current Exact Title A"
    assert binding["paper_record_title"] == "Current Exact Title A"
    assert binding["research_exact_title"] == "Agent Reviewed Research Title A"
    assert binding["research_paper_record_title"] == "Old Extracted Title A"
    assert binding["title_match"] == "agent_reviewed_exact_title_override"
    assert binding["runtime_title_binding"] == "exact_current_record"
    assert repository["paper_evidence"] == original["repositories"][0]["paper_evidence"]
    no_code = document["no_code"][0]
    assert no_code["exact_title"] == "Current Exact Title B"
    assert no_code["research_exact_title"] == "Agent Reviewed Research Title B"
    assert no_code["evidence_urls"] == original["no_code"][0]["evidence_urls"]
    assert source == original
    assert document["audit"]["runtime_binding"]["evidence_id_rebindings"] == 0
    assert document["audit"]["runtime_binding"]["network_or_clone_performed"] is False


def test_missing_or_duplicate_ids_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approvals_path, records_path, source = _fixture(tmp_path, monkeypatch)
    missing = copy.deepcopy(source)
    missing["no_code"] = []
    _write_json(approvals_path, missing)
    with pytest.raises(ValueError, match="sets differ"):
        binder.bind_current_records(
            research_approvals_path=approvals_path,
            paper_record_path=records_path,
        )

    duplicate = copy.deepcopy(source)
    duplicate["no_code"][0]["paper_id"] = "paper-a"
    duplicate["no_code"][0]["title_match"] = "agent_reviewed_exact_title_override"
    _write_json(approvals_path, duplicate)
    with pytest.raises(ValueError, match="appears more than once"):
        binder.bind_current_records(
            research_approvals_path=approvals_path,
            paper_record_path=records_path,
        )


def test_repository_binding_without_same_id_evidence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approvals_path, records_path, source = _fixture(tmp_path, monkeypatch)
    source["repositories"][0]["paper_evidence"][0]["paper_id"] = "paper-b"
    _write_json(approvals_path, source)

    with pytest.raises(ValueError, match="same-ID evidence|same-ID paper binding"):
        binder.bind_current_records(
            research_approvals_path=approvals_path,
            paper_record_path=records_path,
        )


def test_incomplete_no_code_evidence_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approvals_path, records_path, source = _fixture(tmp_path, monkeypatch)
    source["no_code"][0]["evidence_urls"] = []
    _write_json(approvals_path, source)

    with pytest.raises(ValueError, match="incomplete|requires reviewed_at and evidence_urls"):
        binder.bind_current_records(
            research_approvals_path=approvals_path,
            paper_record_path=records_path,
        )


def test_write_runtime_approvals_reloads_with_current_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approvals_path, records_path, _ = _fixture(tmp_path, monkeypatch)
    document = binder.bind_current_records(
        research_approvals_path=approvals_path,
        paper_record_path=records_path,
    )
    output = tmp_path / "runtime-approvals.json"

    binder.write_runtime_approvals(
        document=document,
        output=output,
        paper_record_path=records_path,
    )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["repositories"][0]["papers"][0]["exact_title"] == "Current Exact Title A"
    assert saved["no_code"][0]["exact_title"] == "Current Exact Title B"
