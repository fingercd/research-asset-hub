from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.paper_code_audit import cleanup_staging as cleanup


def _old_tree(path: Path, *, now: float) -> None:
    path.mkdir(parents=True)
    payload = path / "payload.bin"
    payload.write_bytes(b"12345")
    old = now - 3600
    os.utime(payload, (old, old))
    os.utime(path, (old, old))


def test_dry_run_only_selects_direct_staging_children(tmp_path: Path) -> None:
    now = 10_000.0
    staging = tmp_path / ".paper-code-audit-one"
    _old_tree(staging, now=now)
    formal = tmp_path / "github"
    _old_tree(formal, now=now)
    nested = formal / ".paper-code-audit-nested"
    _old_tree(nested, now=now)

    report = cleanup.cleanup_staging(
        archive_root=tmp_path,
        min_age_seconds=60,
        apply=False,
        confirmation=None,
        now=now,
        process_provider=lambda: [],
    )

    assert report["counts"] == {"eligible": 1}
    assert report["items"][0]["size_bytes"] == 5
    assert report["items"][0]["file_count"] == 1
    assert staging.is_dir()
    assert formal.is_dir()
    assert nested.is_dir()


def test_apply_requires_exact_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=cleanup.CONFIRMATION):
        cleanup.cleanup_staging(
            archive_root=tmp_path,
            min_age_seconds=60,
            apply=True,
            confirmation="wrong",
            process_provider=lambda: [],
        )


def test_age_and_process_references_block_removal(tmp_path: Path) -> None:
    now = 10_000.0
    young = tmp_path / ".paper-code-audit-young"
    _old_tree(young, now=now + 3500)
    active = tmp_path / ".paper-code-audit-active"
    _old_tree(active, now=now)
    process = cleanup.ProcessReference(42, "git.exe", f"git -C {active} fetch")

    report = cleanup.cleanup_staging(
        archive_root=tmp_path,
        min_age_seconds=600,
        apply=True,
        confirmation=cleanup.CONFIRMATION,
        now=now,
        process_provider=lambda: [process],
    )

    reasons = {item["name"]: item["reason"] for item in report["items"]}
    assert reasons[young.name] == "younger_than_age_threshold"
    assert reasons[active.name] == "running_process_reference"
    assert young.exists() and active.exists()


def test_confirmed_stale_staging_is_removed_but_formal_directory_is_untouched(
    tmp_path: Path,
) -> None:
    now = 10_000.0
    staging = tmp_path / ".paper-code-audit-stale"
    _old_tree(staging, now=now)
    formal = tmp_path / "github"
    _old_tree(formal, now=now)

    report = cleanup.cleanup_staging(
        archive_root=tmp_path,
        min_age_seconds=60,
        apply=True,
        confirmation=cleanup.CONFIRMATION,
        now=now,
        process_provider=lambda: [],
    )

    assert report["counts"] == {"removed": 1}
    assert not staging.exists()
    assert formal.is_dir()


def test_reparse_candidate_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 10_000.0
    staging = tmp_path / ".paper-code-audit-linklike"
    _old_tree(staging, now=now)
    real_check = cleanup._is_reparse_point
    monkeypatch.setattr(
        cleanup,
        "_is_reparse_point",
        lambda path: path == staging or real_check(path),
    )

    report = cleanup.cleanup_staging(
        archive_root=tmp_path,
        min_age_seconds=60,
        apply=True,
        confirmation=cleanup.CONFIRMATION,
        now=now,
        process_provider=lambda: [],
    )

    assert report["counts"] == {"failed": 1}
    assert staging.exists()
