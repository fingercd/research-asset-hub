from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Asset, Base, RepositoryProfile, Representation, Source
from packages.ingestion.source_integrity import (
    _directory_tree_digest,
    _git_worktree_matches_recorded_revision,
    _only_git_metadata_changed_since,
    verify_source_integrity,
)


def _runtime(tmp_path: Path) -> tuple[Session, SourceRegistry, Path]:
    root = tmp_path / "registered"
    root.mkdir()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    source = Source(
        alias="demo",
        label="Demo",
        adapter="generic",
        root_path=str(root),
    )
    asset = Asset(asset_type="paper", title="Integrity")
    session.add_all([source, asset])
    session.flush()
    session.info["source"] = source
    session.info["asset"] = asset
    session.info["engine"] = engine
    return session, SourceRegistry({("demo", "source"): root.resolve()}), root


def _close(session: Session) -> None:
    engine = session.info["engine"]
    session.close()
    engine.dispose()


def test_file_missing_url_and_report_are_verified_without_absolute_paths(
    tmp_path: Path,
) -> None:
    session, registry, root = _runtime(tmp_path)
    try:
        source = session.info["source"]
        asset = session.info["asset"]
        file_path = root / "paper.pdf"
        file_path.write_bytes(b"%PDF-integrity")
        stat = file_path.stat()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="pdf",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="source",
            path_ref="paper.pdf",
            sha256=hashlib.sha256(file_path.read_bytes()).hexdigest(),
            attributes={
                "_source_size": stat.st_size,
                "_source_mtime_ns": stat.st_mtime_ns,
            },
        )
        missing = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="pdf",
            role="supplemental",
            materialization_kind="file",
            availability="missing",
            root_key="source",
            path_ref="not-found.pdf",
        )
        external = Representation(
            asset_id=asset.id,
            source_id=None,
            representation_type="pdf",
            role="mirror",
            materialization_kind="url",
            availability="available",
            root_key="source",
            path_ref=None,
            external_url="https://example.test/paper.pdf",
        )
        session.add_all([representation, missing, external])
        session.commit()

        report = verify_source_integrity(session, registry).to_dict()

        assert report["status"] == "passed"
        assert report["counts"] == {
            "representations_seen": 3,
            "files_verified": 1,
            "directories_verified": 0,
            "missing_confirmed": 1,
            "external_skipped": 1,
            "mismatches": 0,
        }
        assert report["mismatches"] == []
        rendered = str(report)
        assert str(root.resolve()) not in rendered
        assert "PDF-integrity" not in rendered
    finally:
        _close(session)


def test_file_mutation_and_directory_tree_mismatch_are_reported(tmp_path: Path) -> None:
    session, registry, root = _runtime(tmp_path)
    try:
        source = session.info["source"]
        asset = session.info["asset"]
        file_path = root / "paper.pdf"
        file_path.write_bytes(b"before")
        file_stat = file_path.stat()
        tree = root / "site"
        tree.mkdir()
        (tree / "index.html").write_text("before", encoding="utf-8")
        tree_stat = tree.stat()
        file_representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="pdf",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="source",
            path_ref="paper.pdf",
            sha256=hashlib.sha256(file_path.read_bytes()).hexdigest(),
            attributes={
                "_source_size": file_stat.st_size,
                "_source_mtime_ns": file_stat.st_mtime_ns,
            },
        )
        directory_representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="html",
            role="primary",
            materialization_kind="directory",
            availability="available",
            root_key="source",
            path_ref="site",
            tree_digest=_directory_tree_digest(tree),
            attributes={"_source_mtime_ns": tree_stat.st_mtime_ns},
        )
        session.add_all([file_representation, directory_representation])
        session.commit()

        file_path.write_bytes(b"after!")
        (tree / "index.html").write_text("after", encoding="utf-8")
        report = verify_source_integrity(session, registry).to_dict()

        assert report["status"] == "failed"
        counts = cast(dict[str, int], report["counts"])
        assert counts["mismatches"] == 2
        rows = cast(list[dict[str, object]], report["mismatches"])
        mismatch_reasons = {
            str(item["path_ref"]): set(cast(list[str], item["reasons"]))
            for item in rows
        }
        assert "sha256_mismatch" in mismatch_reasons["paper.pdf"]
        assert "tree_digest_mismatch" in mismatch_reasons["site"]
    finally:
        _close(session)


def test_clean_recorded_git_revision_ignores_only_repository_metadata_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, registry, root = _runtime(tmp_path)
    try:
        source = session.info["source"]
        asset = session.info["asset"]
        tree = root / "repository"
        tree.mkdir()
        metadata = tree / ".git"
        metadata.mkdir()
        (tree / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        tree_stat = tree.stat()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="repository",
            role="original",
            materialization_kind="directory",
            availability="available",
            root_key="source",
            path_ref="repository",
            tree_digest=_directory_tree_digest(tree),
            created_at=datetime.now(UTC) + timedelta(seconds=1),
            attributes={"_source_mtime_ns": tree_stat.st_mtime_ns},
        )
        session.add_all(
            [
                representation,
                RepositoryProfile(
                    asset_id=asset.id,
                    pinned_revision="a" * 40,
                ),
            ]
        )
        session.commit()
        metadata_stat = metadata.stat()
        os.utime(
            metadata,
            ns=(
                metadata_stat.st_atime_ns,
                metadata_stat.st_mtime_ns + 1_000_000,
            ),
        )
        monkeypatch.setattr(
            "packages.ingestion.source_integrity."
            "_git_worktree_matches_recorded_revision",
            lambda path, attributes: True,
        )

        report = verify_source_integrity(session, registry).to_dict()

        assert report["status"] == "passed"
        counts = cast(dict[str, int], report["counts"])
        assert counts["directories_verified"] == 1
        assert counts["git_metadata_drift_ignored"] == 1
        assert counts["mismatches"] == 0
    finally:
        _close(session)


def test_git_lfs_filter_false_positive_requires_raw_head_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    payload = b"committed-full-lfs-payload"
    (repository / "payload.bin").write_bytes(payload)
    commit = "a" * 40

    def fake_run(
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        del kwargs
        if "rev-parse" in arguments:
            return subprocess.CompletedProcess(arguments, 0, f"{commit}\n", "")
        if "status" in arguments:
            return subprocess.CompletedProcess(
                arguments,
                0,
                " M payload.bin\0",
                "",
            )
        if "cat-file" in arguments:
            return subprocess.CompletedProcess(arguments, 0, payload, b"")
        raise AssertionError(arguments)

    monkeypatch.setattr(
        "packages.ingestion.source_integrity.subprocess.run",
        fake_run,
    )

    assert _git_worktree_matches_recorded_revision(
        repository,
        {"commit": commit},
    )

    (repository / "payload.bin").write_bytes(b"changed")
    assert not _git_worktree_matches_recorded_revision(
        repository,
        {"commit": commit},
    )


def test_gitnexus_generated_metadata_is_the_only_allowed_untracked_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / ".gitnexus").mkdir()
    (repository / ".gitnexus" / "index.db").write_bytes(b"generated")
    commit = "b" * 40

    def fake_run(
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        del kwargs
        if "rev-parse" in arguments:
            return subprocess.CompletedProcess(arguments, 0, f"{commit}\n", "")
        if "status" in arguments:
            return subprocess.CompletedProcess(
                arguments,
                0,
                "?? .gitnexus/index.db\0",
                "",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(
        "packages.ingestion.source_integrity.subprocess.run",
        fake_run,
    )
    assert _git_worktree_matches_recorded_revision(repository, {"commit": commit})

    (repository / "untracked.txt").write_text("user content", encoding="utf-8")

    def unsafe_run(
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        del kwargs
        if "rev-parse" in arguments:
            return subprocess.CompletedProcess(arguments, 0, f"{commit}\n", "")
        if "status" in arguments:
            return subprocess.CompletedProcess(
                arguments,
                0,
                "?? .gitnexus/index.db\0?? untracked.txt\0",
                "",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(
        "packages.ingestion.source_integrity.subprocess.run",
        unsafe_run,
    )
    assert not _git_worktree_matches_recorded_revision(
        repository,
        {"commit": commit},
    )


def test_gitnexus_mtime_is_ignored_but_worktree_mtime_is_not(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    metadata = repository / ".gitnexus" / "index.db"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(b"generated")
    baseline = datetime.now(UTC) - timedelta(seconds=1)

    assert _only_git_metadata_changed_since(repository, baseline)

    (repository / "tracked.py").write_text("print('changed')\n", encoding="utf-8")
    assert not _only_git_metadata_changed_since(repository, baseline)
