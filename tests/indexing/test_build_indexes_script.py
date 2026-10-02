from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.domain.models import Asset, Base, Source, SourceRecord, SourceRecordAsset
from packages.indexing.builder import KeywordBuildReport
from scripts import build_indexes


class _ClosingSuite:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FailingBuilder:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def rebuild_keywords(
        self,
        _session: Any,
        *,
        index_names: tuple[str, ...],
    ) -> KeywordBuildReport:
        del index_names
        raise RuntimeError("remote task failed")


def test_failed_build_does_not_publish_success_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "index-build.json"
    suite = _ClosingSuite()
    arguments = argparse.Namespace(
        database_url="sqlite+pysqlite:///:memory:",
        meilisearch_url="http://meili.invalid",
        meilisearch_api_key=None,
        version="v1",
        write_batch_size=17,
        task_flush_batches=2,
        skip_code=False,
        report=report_path,
    )
    monkeypatch.setattr(build_indexes, "_arguments", lambda: arguments)
    monkeypatch.setattr(
        "scripts.build_indexes.RemoteIndexSuite.connect",
        lambda **_kwargs: suite,
    )
    monkeypatch.setattr(build_indexes, "IndexBuilder", _FailingBuilder)

    with pytest.raises(RuntimeError, match="remote task failed"):
        build_indexes.main()

    assert not report_path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert suite.closed is True


def test_skip_code_passes_only_non_code_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "index-build.json"
    suite = _ClosingSuite()
    captured: list[tuple[str, ...]] = []
    connect_kwargs: list[dict[str, Any]] = []

    class CapturingBuilder:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def rebuild_keywords(
            self,
            _session: Any,
            *,
            index_names: tuple[str, ...],
        ) -> KeywordBuildReport:
            captured.append(index_names)
            counts = {name: 0 for name in ("assets", "documents", "code", "annotations")}
            return KeywordBuildReport(
                index_version="v2",
                keyword_counts_before=counts,
                keyword_counts_after=counts,
                keyword_documents_upserted=counts,
                stale_keyword_documents_removed=counts,
                selected_indexes=index_names,
                skipped_indexes=("code",),
            )

    arguments = argparse.Namespace(
        database_url="sqlite+pysqlite:///:memory:",
        meilisearch_url="http://meili.invalid",
        meilisearch_api_key=None,
        version="v2",
        write_batch_size=17,
        task_flush_batches=2,
        skip_code=True,
        report=report_path,
    )

    def fake_connect(**kwargs: Any) -> _ClosingSuite:
        connect_kwargs.append(kwargs)
        return suite

    monkeypatch.setattr(build_indexes, "_arguments", lambda: arguments)
    monkeypatch.setattr(
        "scripts.build_indexes.RemoteIndexSuite.connect",
        fake_connect,
    )
    monkeypatch.setattr(build_indexes, "IndexBuilder", CapturingBuilder)

    build_indexes.main()

    assert captured == [("assets", "documents", "annotations")]
    assert connect_kwargs[0]["index_names"] == (
        "assets",
        "documents",
        "annotations",
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["selected_indexes"] == ["assets", "documents", "annotations"]
    assert report["skipped_indexes"] == ["code"]


def test_success_report_is_atomically_replaced(tmp_path: Path) -> None:
    report_path = tmp_path / "index-build.json"
    report_path.write_text('{"old": true}\n', encoding="utf-8")
    report = KeywordBuildReport(
        index_version="v1",
        keyword_counts_before={"assets": 0},
        keyword_counts_after={"assets": 3},
        keyword_documents_upserted={"assets": 3},
        stale_keyword_documents_removed={"assets": 0},
        batch_size=2,
        peak_buffered_documents=2,
    )

    rendered = build_indexes._write_report_atomic(report_path, report)

    assert json.loads(rendered)["peak_buffered_documents"] == 2
    assert json.loads(report_path.read_text(encoding="utf-8"))["batch_size"] == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_defaults_skip_code_and_accepts_repeated_asset_uuids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = UUID(int=1), UUID(int=2)
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_indexes.py",
            "--asset-id",
            str(first),
            "--asset-id",
            str(second),
            "--source",
            "source",
        ],
    )
    args = build_indexes._arguments()
    assert args.skip_code is True
    assert args.asset_id == [first, second]
    assert args.source == "source"
    monkeypatch.setattr("sys.argv", ["build_indexes.py", "--no-skip-code"])
    assert build_indexes._arguments().skip_code is False


def test_source_scope_calls_refresh_for_primary_and_materialized_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'scope.sqlite'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(alias="source", label="Source", adapter="generic", root_path="/source")
        paper = Asset(asset_type="paper", title="Paper")
        repository = Asset(asset_type="repository", title="Repository")
        other = Asset(asset_type="paper", title="Other")
        session.add_all([source, paper, repository, other])
        session.flush()
        record = SourceRecord(source_id=source.id, source_record_id="paper", asset_id=paper.id)
        session.add(record)
        session.flush()
        session.add(
            SourceRecordAsset(
                source_record_id=record.id,
                asset_id=repository.id,
                role="repository",
            )
        )
        expected_ids = {paper.id, repository.id}
        session.commit()
    engine.dispose()
    captured: list[tuple[UUID, ...]] = []
    suite = _ClosingSuite()

    class Builder:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def refresh_keywords(
            self,
            _session: Session,
            *,
            asset_ids: tuple[UUID, ...],
            index_names: tuple[str, ...],
        ) -> KeywordBuildReport:
            captured.append(asset_ids)
            assert index_names == ("assets", "documents", "annotations")
            counts = {name: 1 for name in index_names}
            return KeywordBuildReport(
                index_version="v1",
                keyword_counts_before=counts,
                keyword_counts_after=counts,
                keyword_documents_upserted=counts,
                stale_keyword_documents_removed=counts,
                asset_ids=tuple(str(value) for value in asset_ids),
                operation="refresh",
                selected_indexes=index_names,
            )

    report_path = tmp_path / "scope-report.json"
    arguments = argparse.Namespace(
        database_url=database_url,
        meilisearch_url="http://invalid",
        meilisearch_api_key=None,
        version="v1",
        write_batch_size=17,
        task_flush_batches=2,
        skip_code=True,
        asset_id=None,
        source="source",
        report=report_path,
    )
    monkeypatch.setattr(build_indexes, "_arguments", lambda: arguments)
    monkeypatch.setattr(build_indexes, "IndexBuilder", Builder)
    monkeypatch.setattr("scripts.build_indexes.RemoteIndexSuite.connect", lambda **kwargs: suite)
    build_indexes.main()
    assert len(captured) == 1 and set(captured[0]) == expected_ids
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["operation"] == "refresh"
    assert set(report["asset_ids"]) == {str(value) for value in expected_ids}
    assert suite.closed is True
