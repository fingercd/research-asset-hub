from __future__ import annotations

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
    ]


def test_inventory_accounts_for_428_source_records() -> None:
    report = build_inventory_report(load_sources())

    assert report["status"] == "passed"
    assert report["totals"]["source_count"] == 6
    assert report["totals"]["source_record_count"] == 428
    assert report["totals"]["declared_pdf_files"] >= 427
    assert report["totals"]["declared_html_files"] >= 500


def test_first_checkpoint_refuses_non_dry_run_ingest() -> None:
    assert main(["ingest", "--all"]) == 2
