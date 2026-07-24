from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.backup.release_report import (
    ReleaseReportInputs,
    build_release_report,
    collect_database_snapshot,
    render_release_markdown,
    write_release_report,
)
from packages.domain.models import Base, Source
from scripts.generate_release_report import _discover_reports

_IDEMPOTENCY_COUNTERS = (
    "source_records_created",
    "source_record_versions_created",
    "paper_assets_created",
    "repository_assets_created",
    "representations_created",
    "relations_created",
    "relation_evidence_created",
    "field_claims_created",
)
_REQUIRED_FORMAL_COUNTERS = (
    *_IDEMPOTENCY_COUNTERS,
    "relation_candidates",
    "relation_candidates_resolved",
    "relation_candidates_unresolved",
    "representation_candidates",
    "representations_available",
    "representations_missing",
    "unapproved_merge_groups",
)


def _database() -> dict[str, object]:
    return {
        "schema_revision": "0003_search_rag_integrations",
        "sources": {
            "count": 7,
            "all_read_only": True,
            "items": [
                *[
                    {
                        "alias": f"legacy-{index}",
                        "read_only": True,
                        "enabled": True,
                        "frozen_manifest_sha256": f"{index:x}" * 64,
                    }
                    for index in range(6)
                ],
                {
                    "alias": "new-domain",
                    "read_only": True,
                    "enabled": True,
                    "frozen_manifest_sha256": "b" * 64,
                },
            ],
        },
        "assets": {
            "total": 801,
            "active": 799,
            "soft_deleted": 2,
            "by_type": {"paper": 483, "repository": 316},
            "domains": {f"domain-{index}": 1 for index in range(7)},
            "domain_count": 7,
        },
        "source_records": {
            "total": 518,
            "versions": 518,
            "asset_links": 900,
            "by_outcome": {"confirmed": 518},
        },
        "representations": {
            "total": 2408,
            "active": 2408,
            "by_type": {"pdf": 500, "html": 637, "repository": 300},
            "by_availability": {"available": 2399, "missing": 9},
            "network_required": 2,
            "html_candidates": 637,
            "missing": [
                {
                    "representation_id": f"missing-{index}",
                    "asset_id": f"asset-{index}",
                    "type": "pdf",
                    "root_key": "pdf",
                    "path_ref": f"missing/{index}.pdf",
                }
                for index in range(9)
            ],
        },
        "relations": {
            "by_type": {"implemented_by": 330},
            "by_status": {"confirmed": 330},
            "evidence_total": 400,
            "implemented_by_evidence": 340,
            "dangling": 0,
            "logical_duplicates": 0,
            "orphan_evidence": 0,
        },
        "processing": {
            "extract_jobs_by_status": {"succeeded": 2399, "failed": 2},
            "ocr_states": {"not_needed": 2300, "succeeded": 99},
            "chunks_total": 9999,
            "failed_jobs": [
                {
                    "job_id": "job-1",
                    "job_type": "extract_representation",
                    "error_code": "UnsupportedFormat",
                }
            ],
            "failed_activities": [],
        },
        "indexes": {"by_status": {"ready": 9999}, "failed": []},
        "integrations": {"pending_conflicts": [], "sync_conflicts": {}},
        "backup": {
            "runs": [
                {
                    "backup_id": "backup-1",
                    "status": "verified",
                    "local_copy_only": True,
                    "verified": True,
                }
            ]
        },
        "audit": {
            "events_total": 100,
            "outbox_total": 100,
            "outbox_by_status": {"published": 100},
        },
    }


def _inventory(*, source_count: int, records: int, include_new: bool) -> dict[str, object]:
    sources: list[dict[str, object]] = [
        {
            "id": f"legacy-{index}",
            "read_only": True,
            "manifest": {
                "records": 428 // 6,
                "sha256": f"{index:x}" * 64,
                "mtime_ns": index + 1,
            },
        }
        for index in range(6)
    ]
    if include_new:
        sources.append(
            {
                "id": "new-domain",
                "read_only": True,
                "manifest": {
                    "records": 90,
                    "sha256": "b" * 64,
                    "mtime_ns": 2,
                },
            }
        )
    return {
        "status": "passed",
        "sources": sources,
        "totals": {
            "source_count": source_count,
            "source_record_count": records,
        },
    }


def _reports() -> dict[str, dict[str, object]]:
    return {
        "inventory_current": _inventory(
            source_count=7, records=518, include_new=True
        ),
        "inventory_baseline": _inventory(
            source_count=6, records=428, include_new=False
        ),
        "formal_import": {
            "schema_version": 2,
            "status": "passed",
            "source_roots_modified": False,
            "validated_dry_run": {
                "status": "passed",
                "generated_at": "2026-07-25T00:00:00+00:00",
                "normalized_records": 518,
                "unapproved_review_groups": 0,
            },
            "counters": {
                "source_records_seen": 518,
                "source_records_created": 0,
                "source_record_versions_created": 0,
                "paper_assets_created": 0,
                "repository_assets_created": 0,
                "representations_created": 0,
                "representations_reused": 2408,
                "relations_created": 0,
                "relation_evidence_created": 0,
                "field_claims_created": 0,
                "paper_assets_total": 483,
                "repository_assets_total": 316,
                "source_record_candidates": 518,
                "relation_candidates": 1753,
                "relation_candidates_resolved": 385,
                "relation_candidates_unresolved": 1368,
                "representation_candidates": 2408,
                "representations_available": 2399,
                "representations_missing": 9,
                "strong_merge_groups": 25,
                "reviewed_merge_groups": 5,
                "unapproved_merge_groups": 0,
            },
        },
        "source_integrity": {
            "status": "passed",
            "read_only": True,
            "counts": {
                "representations_seen": 2408,
                "files_verified": 2399,
                "directories_verified": 0,
                "missing_confirmed": 9,
                "external_skipped": 0,
                "mismatches": 0,
            },
            "mismatches": [],
        },
        "html_audit": {
            "status": "completed",
            "selection": {"registered_html_candidates": 637},
            "summary": {
                "static_statuses": {"audited": 637},
                "inferred_policies": {"static": 635, "network_required": 2},
                "policy_mismatches": 0,
                "active_external_dependencies": 2,
                "missing_local_dependencies": 0,
                "dynamic_requested": 12,
                "dynamic_completed": 12,
                "dynamic_probe_status": "completed",
            },
            "representations": [
                {
                    "representation_id": "html-1",
                    "asset_id": "asset-1",
                    "static": {"inferred_policy": "network_required"},
                }
            ],
        },
        "index_build": {
            "status": "passed",
            "index_version": "bge_m3_v1",
            "embedding_provider": "sentence-transformers",
            "embedding_model": "BAAI/bge-m3",
            "embedding_dimension": 1024,
            "degraded": False,
            "keyword_counts_after": {"assets": 799},
            "vector_counts_after": {"rah_assets_bge_m3_v1": 799},
        },
        "search_evaluation": {
            "status": "passed",
            "metrics": {
                "exact_recall_at_5": 1.0,
                "recall_at_10": 0.9,
                "ndcg_at_10": 0.8,
            },
            "checks": {"exact_recall_at_5": True},
        },
        "rag_evaluation": {
            "status": "passed",
            "question_count": 30,
            "metrics": {"citation_coverage": 1.0, "invalid_citations": 0},
            "checks": {"citation_coverage": True},
        },
        "recovery_fixture": {
            "schema": "research-asset-hub.recovery-fixture/v1",
            "status": "verified",
        },
        "restore_rehearsal": {
            "status": "verified",
            "snapshot_id": "snapshot",
            "safety": {"active_resources_mutated": False},
            "manifest_validation": {"status": "verified", "files_checked": 100},
            "database": {
                "validation": {"status": "verified"},
                "restored": {
                    "counts": {
                        "annotations": 1,
                        "annotation_revisions": 2,
                        "reading_states": 1,
                        "sync_conflicts": 1,
                    }
                },
            },
            "index_rebuild": {
                "status": "verified",
                "fixed_query": {
                    "keyword_exact_hit": True,
                    "vector_self_hit": True,
                },
            },
            "cleanup": {"status": "verified"},
        },
    }


def _gate(report: dict[str, object], identifier: str) -> dict[str, object]:
    gates = report["gates"]
    assert isinstance(gates, list)
    result = next(
        item
        for item in gates
        if isinstance(item, dict) and item.get("id") == identifier
    )
    return result


def test_new_domain_growth_does_not_fail_legacy_exact_counts() -> None:
    report = build_release_report(
        ReleaseReportInputs(
            database=_database(),
            reports=_reports(),
            generated_at=datetime(2026, 7, 24, tzinfo=UTC),
        )
    )
    legacy_gate = next(
        item for item in report["gates"] if item["id"] == "legacy-asset-floor"
    )
    fingerprint_gate = next(
        item for item in report["gates"] if item["id"] == "source-fingerprints"
    )
    assert legacy_gate["status"] == "pass"
    assert fingerprint_gate["status"] == "pass"
    assert report["scope"]["growth"]["domain_delta"] == 1
    assert report["source_integrity"]["added_sources"] == ["new-domain"]
    assert report["status"] == "warn"


def test_database_snapshot_projects_frozen_manifest_digest_without_source_config() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Source(
                alias="intake-example",
                label="Example",
                adapter="generic_manifest",
                root_path="config/intake/intake-example",
                manifest_path="manifest.json",
                read_only=True,
                enabled=True,
                config={
                    "_frozen_manifest_sha256": "A" * 64,
                    "private_config_value": "must-not-be-exported",
                },
            )
        )
        session.commit()

        snapshot = collect_database_snapshot(session)

    source_items = snapshot["sources"]["items"]
    assert source_items == [
        {
            "alias": "intake-example",
            "read_only": True,
            "enabled": True,
            "frozen_manifest_sha256": "a" * 64,
        }
    ]
    assert "private_config_value" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "failure_kind,expected_field",
    [
        ("missing_frozen", "missing_frozen_fingerprints"),
        ("mismatch", "frozen_fingerprint_mismatches"),
        ("missing_database_source", "missing_database_sources"),
        ("missing_inventory_source", "missing_inventory_sources"),
    ],
)
def test_source_fingerprint_gate_requires_frozen_digest_for_every_current_source(
    failure_kind: str,
    expected_field: str,
) -> None:
    database = _database()
    reports = _reports()
    database_sources = database["sources"]
    assert isinstance(database_sources, dict)
    database_items = database_sources["items"]
    assert isinstance(database_items, list)
    current_inventory = reports["inventory_current"]
    current_sources = current_inventory["sources"]
    assert isinstance(current_sources, list)

    if failure_kind == "missing_inventory_source":
        current_sources[:] = [
            item
            for item in current_sources
            if isinstance(item, dict) and item.get("id") != "new-domain"
        ]
    else:
        new_database_source = next(
            item
            for item in database_items
            if isinstance(item, dict) and item.get("alias") == "new-domain"
        )
        if failure_kind == "missing_frozen":
            new_database_source.pop("frozen_manifest_sha256")
        elif failure_kind == "mismatch":
            new_database_source["frozen_manifest_sha256"] = "c" * 64
        else:
            database_items.remove(new_database_source)

    report = build_release_report(
        ReleaseReportInputs(database=database, reports=reports)
    )
    gate = _gate(report, "source-fingerprints")
    observed = gate["observed"]
    assert isinstance(observed, dict)

    assert gate["status"] == "fail"
    assert observed[expected_field]
    assert report["status"] == "fail"


def test_source_representation_mismatch_is_a_release_failure() -> None:
    reports = _reports()
    source_integrity = reports["source_integrity"]
    source_integrity["status"] = "failed"
    counts = source_integrity["counts"]
    assert isinstance(counts, dict)
    counts["files_verified"] = 2398
    counts["mismatches"] = 1
    source_integrity["mismatches"] = [
        {
            "representation_id": "rep-1",
            "source_alias": "legacy",
            "path_ref": "papers/paper.pdf",
            "reasons": ["sha256_mismatch"],
        }
    ]

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = next(
        item
        for item in report["gates"]
        if item["id"] == "source-representation-integrity"
    )

    assert gate["status"] == "fail"
    assert report["status"] == "fail"
    assert report["failures"]["source_representation_mismatches"][0][
        "reasons"
    ] == ["sha256_mismatch"]


def test_unapproved_weak_merge_is_a_release_failure() -> None:
    reports = _reports()
    formal_import = reports["formal_import"]
    counters = formal_import["counters"]
    assert isinstance(counters, dict)
    counters["unapproved_merge_groups"] = 1

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = next(
        item for item in report["gates"] if item["id"] == "idempotent-import"
    )

    assert gate["status"] == "fail"
    assert report["status"] == "fail"


@pytest.mark.parametrize("schema_version", [None, 0, 1, 3, "2", True])
def test_formal_import_gate_rejects_missing_or_old_schema(
    schema_version: object,
) -> None:
    reports = _reports()
    formal_import = reports["formal_import"]
    if schema_version is None:
        formal_import.pop("schema_version")
    else:
        formal_import["schema_version"] = schema_version

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = _gate(report, "idempotent-import")
    observed = gate["observed"]
    assert isinstance(observed, dict)

    assert gate["status"] == "fail"
    assert observed["schema_valid"] is False
    assert report["status"] == "fail"


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("validated_dry_run", None),
        ("status", None),
        ("status", "failed"),
        ("unapproved_review_groups", None),
        ("unapproved_review_groups", 1),
        ("unapproved_review_groups", True),
        ("unapproved_review_groups", "0"),
    ],
)
def test_formal_import_gate_requires_successful_validated_dry_run(
    field_name: str,
    field_value: object,
) -> None:
    reports = _reports()
    formal_import = reports["formal_import"]
    if field_name == "validated_dry_run":
        formal_import.pop(field_name)
    else:
        validated_dry_run = formal_import["validated_dry_run"]
        assert isinstance(validated_dry_run, dict)
        if field_value is None:
            validated_dry_run.pop(field_name)
        else:
            validated_dry_run[field_name] = field_value

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = _gate(report, "idempotent-import")
    observed = gate["observed"]
    assert isinstance(observed, dict)
    validated_observed = observed["validated_dry_run"]
    assert isinstance(validated_observed, dict)

    assert gate["status"] == "fail"
    assert validated_observed["valid"] is False
    assert report["status"] == "fail"


@pytest.mark.parametrize("counter_name", _REQUIRED_FORMAL_COUNTERS)
def test_formal_import_gate_rejects_each_missing_required_counter(
    counter_name: str,
) -> None:
    reports = _reports()
    formal_import = reports["formal_import"]
    counters = formal_import["counters"]
    assert isinstance(counters, dict)
    counters.pop(counter_name)

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = _gate(report, "idempotent-import")
    observed = gate["observed"]
    assert isinstance(observed, dict)

    assert gate["status"] == "fail"
    assert counter_name in observed["missing_required_counters"]
    assert report["status"] == "fail"


@pytest.mark.parametrize(
    "counter_name,invalid_value",
    [
        ("source_records_created", -1),
        ("source_record_versions_created", True),
        ("field_claims_created", 0.0),
        ("unapproved_merge_groups", "0"),
    ],
)
def test_formal_import_gate_rejects_invalid_required_counter(
    counter_name: str,
    invalid_value: object,
) -> None:
    reports = _reports()
    formal_import = reports["formal_import"]
    counters = formal_import["counters"]
    assert isinstance(counters, dict)
    counters[counter_name] = invalid_value

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = _gate(report, "idempotent-import")
    observed = gate["observed"]
    assert isinstance(observed, dict)

    assert gate["status"] == "fail"
    assert counter_name in observed["invalid_required_counters"]
    assert report["status"] == "fail"


@pytest.mark.parametrize("counter_name", _IDEMPOTENCY_COUNTERS)
def test_formal_import_gate_rejects_each_nonzero_idempotency_counter(
    counter_name: str,
) -> None:
    reports = _reports()
    formal_import = reports["formal_import"]
    counters = formal_import["counters"]
    assert isinstance(counters, dict)
    counters[counter_name] = 1

    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=reports)
    )
    gate = _gate(report, "idempotent-import")
    observed = gate["observed"]
    assert isinstance(observed, dict)
    created_counters = observed["created_counters"]
    assert isinstance(created_counters, dict)

    assert gate["status"] == "fail"
    assert created_counters[counter_name] == 1
    assert report["status"] == "fail"


def test_report_exposes_failures_without_private_text_or_absolute_paths() -> None:
    database = _database()
    processing = database["processing"]
    assert isinstance(processing, dict)
    processing["failed_jobs"] = [
        {
            "job_id": "job-1",
            "job_type": "extract_representation",
            "entity_ids": {"representation_id": "rep-1"},
            "error_code": "DataError",
        }
    ]
    database["database_url"] = "postgresql://user:password@localhost/private"
    database["root_path"] = r"D:\private\papers"
    database["text_content"] = "private paper body"
    report = build_release_report(
        ReleaseReportInputs(database=database, reports=_reports())
    )
    rendered = json.dumps(report, ensure_ascii=False)
    assert "DataError" in rendered
    assert "postgresql://" not in rendered
    assert "private paper body" not in rendered
    assert "D:\\\\" not in rendered
    assert "password@localhost" not in rendered
    assert report["privacy"]["contains_private_document_text"] is False


def test_write_json_and_markdown_reports(tmp_path: Path) -> None:
    report = build_release_report(
        ReleaseReportInputs(database=_database(), reports=_reports())
    )
    validation = tmp_path / "work" / "validation" / "release-report.json"
    artifacts = write_release_report(
        report,
        output_directory=tmp_path / "outputs" / "release",
        validation_path=validation,
    )
    json_path = Path(artifacts["json"])
    markdown_path = Path(artifacts["markdown"])
    assert json.loads(json_path.read_text(encoding="utf-8"))["schema"].endswith(
        "/v1"
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "新增第七领域" in markdown
    assert "发布门禁" in markdown
    assert validation.read_text(encoding="utf-8") == json_path.read_text(
        encoding="utf-8"
    )
    assert render_release_markdown(report).startswith(
        "# Research Asset Hub v1 发布报告"
    )


def test_release_discovery_prefers_production_html_and_bge_index(
    tmp_path: Path,
) -> None:
    validation = tmp_path / "validation"
    validation.mkdir()
    fixtures = {
        "html-audit.json": {"kind": "static"},
        "html-audit-production.json": {"kind": "production"},
        "index-build-keywords.json": {"kind": "keywords"},
        "index-build-bge-m3.json": {"kind": "bge"},
        "source-integrity.json": {"kind": "source-integrity"},
    }
    for name, document in fixtures.items():
        (validation / name).write_text(
            json.dumps(document),
            encoding="utf-8",
        )

    reports, discovered = _discover_reports(validation, restore_report=None)

    assert reports["html_audit"]["kind"] == "production"
    assert discovered["html_audit"] == "html-audit-production.json"
    assert reports["index_build"]["kind"] == "bge"
    assert discovered["index_build"] == "index-build-bge-m3.json"
    assert reports["source_integrity"]["kind"] == "source-integrity"
