from __future__ import annotations

from packages.cli.config import load_sources
from packages.cli.inspection import sha256_file
from packages.ingestion.dry_run import run_dry_run


def test_full_dry_run_accounts_for_sources_without_modifying_manifests() -> None:
    sources = load_sources()
    before = {source.source_id: sha256_file(source.manifest) for source in sources}

    report = run_dry_run(sources)

    after = {source.source_id: sha256_file(source.manifest) for source in sources}
    expected_records = sum(source.expected_records or 0 for source in sources)
    assert report["status"] == "passed"
    assert report["errors"] == []
    assert report["commit_performed"] is False
    assert report["source_roots_modified"] is False
    assert report["totals"]["configured_sources"] == len(sources)
    assert report["totals"]["normalized_records"] == expected_records == 428
    assert report["reconciliation"]["unique_source_record_keys"] == expected_records
    assert report["reconciliation"]["duplicate_source_record_keys"] == []
    assert before == after

    reinforcement_learning = next(
        item for item in report["sources"] if item["source_id"] == "reinforcement_learning"
    )
    assert "path_outside_root" not in reinforcement_learning["warnings"]
    assert reinforcement_learning["representations"]["repository"] == 72
