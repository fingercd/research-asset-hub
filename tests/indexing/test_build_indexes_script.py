from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

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
