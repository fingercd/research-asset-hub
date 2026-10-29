from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tools.paper_code_audit import audit


def _approval(url: str, destination: str) -> audit.RepositoryApproval:
    link = audit.canonical_link(url)
    assert link is not None
    return audit.RepositoryApproval(
        link=link,
        commit="a" * 40,
        destination=destination,
        papers=(audit.ApprovedPaper(destination.replace("/", "-"), "Paper"),),
        sparse_paths=(),
        evidence=("reviewed",),
    )


def _approval_set(*approvals: audit.RepositoryApproval) -> audit.ApprovalSet:
    return audit.ApprovalSet(repositories={approval.link.key: approval for approval in approvals})


def test_missing_clones_run_concurrently_fail_independently_and_sort_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approvals = _approval_set(
        _approval("https://github.com/example/repo-c", "github/example/repo-c"),
        _approval("https://github.com/example/repo-a", "github/example/repo-a"),
        _approval("https://github.com/example/repo-b", "github/example/repo-b"),
    )
    barrier = threading.Barrier(3, timeout=5)
    seen_threads: set[int] = set()
    lock = threading.Lock()

    def fake_clone(approval: audit.RepositoryApproval, *, archive_root: Path) -> dict[str, object]:
        assert archive_root == tmp_path
        with lock:
            seen_threads.add(threading.get_ident())
        barrier.wait()
        if approval.link.key.endswith("repo-b"):
            raise RuntimeError("isolated worker failure")
        return {
            "status": "archived",
            "path": str(tmp_path / approval.destination),
        }

    monkeypatch.setattr(audit, "_execute_clone", fake_clone)

    results = audit.apply_approved_clones(
        approvals,
        archive_root=tmp_path,
        repository_index={},
        clone_jobs=3,
    )

    assert len(seen_threads) == 3
    assert [item["canonical_key"] for item in results] == sorted(approvals.repositories)
    by_key = {item["canonical_key"]: item for item in results}
    assert by_key["github.com/example/repo-a"]["status"] == "archived"
    assert by_key["github.com/example/repo-c"]["status"] == "archived"
    assert by_key["github.com/example/repo-b"]["status"] == "failed"
    assert by_key["github.com/example/repo-b"]["reason"] == "clone_worker_exception"


def test_duplicate_missing_destinations_never_enter_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _approval("https://github.com/example/first", "github/shared/repo")
    second = _approval("https://github.com/example/second", "GITHUB/SHARED/REPO")
    unique = _approval("https://github.com/example/unique", "github/example/unique")
    approvals = _approval_set(first, second, unique)
    executed = []

    def fake_clone(approval: audit.RepositoryApproval, *, archive_root: Path) -> dict[str, object]:
        executed.append(approval.link.key)
        return {"status": "archived", "path": str(archive_root / approval.destination)}

    monkeypatch.setattr(audit, "_execute_clone", fake_clone)

    results = audit.apply_approved_clones(
        approvals,
        archive_root=tmp_path,
        repository_index={},
        clone_jobs=3,
    )

    assert executed == [unique.link.key]
    by_key = {item["canonical_key"]: item for item in results}
    for key in (first.link.key, second.link.key):
        assert by_key[key]["status"] == "failed"
        assert by_key[key]["reason"] == "duplicate_destination"
        assert by_key[key]["conflicting_canonical_keys"] == sorted(
            [first.link.key, second.link.key]
        )
    assert by_key[unique.link.key]["status"] == "archived"


def test_existing_repository_is_reused_without_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = _approval("https://github.com/example/existing", "github/example/existing")
    approvals = _approval_set(existing)
    repository_path = tmp_path / "already-there"
    repository_path.mkdir()

    def unexpected_clone(
        approval: audit.RepositoryApproval, *, archive_root: Path
    ) -> dict[str, object]:
        raise AssertionError((approval, archive_root))

    monkeypatch.setattr(audit, "_execute_clone", unexpected_clone)

    results = audit.apply_approved_clones(
        approvals,
        archive_root=tmp_path,
        repository_index={existing.link.key: [repository_path]},
        clone_jobs=8,
    )

    assert results == [
        {
            "canonical_key": existing.link.key,
            "status": "reused_existing",
            "paths": [str(repository_path.resolve())],
        }
    ]


@pytest.mark.parametrize("clone_jobs", [0, 9])
def test_clone_job_range_is_fail_closed(tmp_path: Path, clone_jobs: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 8"):
        audit.apply_approved_clones(
            audit.ApprovalSet(),
            archive_root=tmp_path,
            repository_index={},
            clone_jobs=clone_jobs,
        )
