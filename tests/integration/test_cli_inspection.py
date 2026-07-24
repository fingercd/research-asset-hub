from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from packages.cli.config import load_sources
from packages.cli.inspection import build_inventory_report, build_preflight_report
from packages.cli.main import main


def test_all_configured_sources_are_read_only_and_present() -> None:
    sources = load_sources()

    assert [source.source_id for source in sources] == [
        "agent",
        "infra",
        "world_model",
        "reinforcement_learning",
        "fine_tuning",
        "generation",
        "llm_tech_reports",
    ]
    assert all(source.read_only for source in sources)
    assert all(source.root.is_dir() for source in sources)
    assert all(source.manifest.is_file() for source in sources)


def test_preflight_accounts_for_all_source_records() -> None:
    report = build_preflight_report(load_sources())

    assert report["status"] == "ready_with_warnings"
    assert report["blocking_errors"] == []
    assert [source["record_count"] for source in report["sources"]] == [
        72,
        71,
        46,
        108,
        67,
        64,
        90,
    ]


def test_inventory_accounts_for_518_source_records() -> None:
    report = build_inventory_report(load_sources())

    assert report["status"] == "passed"
    assert report["totals"]["source_count"] == 7
    assert report["totals"]["source_record_count"] == 518
    assert report["totals"]["declared_pdf_files"] >= 427
    assert report["totals"]["declared_html_files"] >= 500


def test_formal_ingest_uses_the_configured_database_after_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, int]] = []

    def fake_formal_import(session: Any, sources: Any) -> dict[str, Any]:
        calls.append((session.bind.dialect.name, len(sources)))
        return {
            "schema_version": 1,
            "status": "passed",
            "source_roots_modified": False,
        }

    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(
        "packages.ingestion.formal.run_formal_import",
        fake_formal_import,
    )
    monkeypatch.setattr(
        "packages.cli.main.write_report",
        lambda report, name: tmp_path / name,
    )

    assert main(["ingest", "--all"]) == 0
    assert calls == [("sqlite", 7)]
