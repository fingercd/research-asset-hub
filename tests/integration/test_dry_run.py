from __future__ import annotations

import json
from pathlib import Path

from packages.cli.config import LoadedSource, load_sources
from packages.cli.inspection import sha256_file
from packages.ingestion.dry_run import run_dry_run

BASELINE_MIN_RECORDS = 518


def test_full_dry_run_accounts_for_sources_without_modifying_manifests(
    local_research_assets: None,
) -> None:
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
    assert report["totals"]["normalized_records"] == expected_records
    assert report["totals"]["normalized_records"] >= BASELINE_MIN_RECORDS
    assert report["reconciliation"]["unique_source_record_keys"] == expected_records
    assert report["reconciliation"]["duplicate_source_record_keys"] == []
    assert before == after

    reinforcement_learning = next(
        item
        for item in report["sources"]
        if item["adapter"] == "reinforcement_learning"
    )
    assert "path_outside_root" not in reinforcement_learning["warnings"]
    assert reinforcement_learning["representations"]["repository"] > 0


def test_dry_run_fails_for_unapproved_cross_source_title_year_merge(
    tmp_path: Path,
) -> None:
    sources: list[LoadedSource] = []
    for source_id in ("left", "right"):
        root = tmp_path / source_id
        root.mkdir()
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": f"{source_id}-paper",
                            "title": "Unapproved Shared Title",
                            "year": 2026,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        sources.append(
            LoadedSource(
                source_id=source_id,
                label=source_id,
                domain="test-domain",
                adapter="generic_manifest",
                root=root,
                manifest=manifest,
                expected_records=1,
                read_only=True,
                raw={
                    "id": source_id,
                    "label": source_id,
                    "domain": "test-domain",
                    "adapter": "generic_manifest",
                    "root": str(root),
                    "manifest": "manifest.json",
                    "options": {"record_selector": "items"},
                },
            )
        )

    report = run_dry_run(sources, known_domain_slugs={"test-domain"})

    assert report["status"] == "failed"
    assert (
        report["reconciliation"]["unapproved_review_groups"][0][
            "approval_status"
        ]
        == "not_allowlisted"
    )
