from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from packages.cli.config import load_domain_slugs, load_sources
from packages.cli.inspection import build_inventory_report, build_preflight_report
from packages.cli.main import main

BASELINE_SOURCE_IDS = frozenset(
    {
        "agent",
        "infra",
        "world_model",
        "reinforcement_learning",
        "fine_tuning",
        "generation",
        "llm_tech_reports",
    }
)
BASELINE_MIN_RECORDS = 518


def test_all_configured_sources_are_read_only_and_present() -> None:
    sources = load_sources()

    configured_ids = {source.source_id for source in sources}
    assert BASELINE_SOURCE_IDS <= configured_ids
    assert len(configured_ids) >= len(BASELINE_SOURCE_IDS)
    assert all(source.read_only for source in sources)
    assert all(source.root.is_dir() for source in sources)
    assert all(source.manifest.is_file() for source in sources)
    assert {source.domain for source in sources}.issubset(load_domain_slugs())


def test_preflight_accounts_for_all_source_records() -> None:
    sources = load_sources()
    report = build_preflight_report(sources)

    assert report["status"] == "ready_with_warnings"
    assert report["blocking_errors"] == []
    configured = {source.source_id: source for source in sources}
    observed = {source["id"]: source for source in report["sources"]}
    assert observed.keys() == configured.keys()
    assert all(
        observed[source_id]["record_count"] == source.expected_records
        for source_id, source in configured.items()
        if source.expected_records is not None
    )
    assert sum(item["record_count"] or 0 for item in observed.values()) >= BASELINE_MIN_RECORDS


def test_inventory_accounts_for_configured_source_records() -> None:
    report = build_inventory_report(load_sources())

    assert report["status"] == "passed"
    assert report["totals"]["source_count"] >= len(BASELINE_SOURCE_IDS)
    assert report["totals"]["source_record_count"] >= BASELINE_MIN_RECORDS
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
    assert calls == [("sqlite", len(load_sources()))]


def test_formal_ingest_rejects_partial_source_scope_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "packages.cli.main.write_report",
        lambda report, name: tmp_path / name,
    )

    assert main(["ingest", "--source", "agent", "--json"]) == 1
    output = capsys.readouterr().out
    assert "PartialFormalImportForbidden" in output
    assert "complete source set" in output
