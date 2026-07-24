from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from packages.backup.release_report import (
    ReleaseReportInputs,
    build_release_report,
    render_release_markdown,
    write_release_report,
)
from scripts.generate_release_report import _discover_reports


def _database() -> dict[str, object]:
    return {
        "schema_revision": "0003_search_rag_integrations",
        "sources": {
            "count": 7,
            "all_read_only": True,
            "items": [{"alias": "legacy", "read_only": True, "enabled": True}],
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
            "status": "passed",
            "source_roots_modified": False,
            "counters": {
                "source_records_created": 0,
                "paper_assets_created": 0,
                "repository_assets_created": 0,
                "representations_created": 0,
                "relations_created": 0,
                "relation_evidence_created": 0,
                "relation_candidates": 1753,
                "relation_candidates_resolved": 385,
                "relation_candidates_unresolved": 1368,
                "representation_candidates": 2408,
                "representations_available": 2399,
                "representations_missing": 9,
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
