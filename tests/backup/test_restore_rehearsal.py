from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from packages.backup import rehearsal
from packages.backup.rehearsal import (
    CORE_TABLES,
    RestorePlan,
    RestorePorts,
    RestoreSafetyError,
    compare_database_snapshots,
    discover_restored_layout,
    render_restore_compose,
    run_restore_rehearsal,
    verify_restored_manifest,
)


def _plan(project_root: Path, suffix: str = "safe-run") -> RestorePlan:
    return RestorePlan(
        project_root=project_root,
        run_root=project_root / "work" / "restore-tests" / suffix,
        compose_project=f"rah-restore-{suffix}",
    )


def test_restore_plan_rejects_target_outside_workspace_restore_root(
    tmp_path: Path,
) -> None:
    plan = RestorePlan(
        project_root=tmp_path / "project",
        run_root=tmp_path / "outside",
        compose_project="rah-restore-safe-run",
    )
    with pytest.raises(RestoreSafetyError, match="work/restore-tests"):
        plan.validate()


@pytest.mark.parametrize(
    ("project_name", "database_name", "ports"),
    [
        ("research-asset-hub", "research_asset_hub_restore", RestorePorts()),
        ("unsafe", "research_asset_hub_restore", RestorePorts()),
        ("rah-restore-safe-run", "research_asset_hub", RestorePorts()),
        (
            "rah-restore-safe-run",
            "research_asset_hub_restore",
            RestorePorts(postgres=5432),
        ),
    ],
)
def test_restore_plan_rejects_active_or_non_namespaced_resources(
    tmp_path: Path,
    project_name: str,
    database_name: str,
    ports: RestorePorts,
) -> None:
    project_root = tmp_path / "project"
    plan = RestorePlan(
        project_root=project_root,
        run_root=project_root / "work" / "restore-tests" / "safe-run",
        compose_project=project_name,
        database_name=database_name,
        ports=ports,
    )
    with pytest.raises(RestoreSafetyError):
        plan.validate()


def test_compose_uses_loopback_and_project_scoped_named_volumes(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "project")
    compose = render_restore_compose(plan)
    assert "127.0.0.1:55432:5432" in compose
    assert "127.0.0.1:57700:7700" in compose
    assert "postgres-restore-data:/var/lib/postgresql/data" in compose
    assert "external: true" not in compose
    assert "research_asset_hub_restore" in compose


def test_manifest_discovery_and_digest_verification(tmp_path: Path) -> None:
    restored_project = tmp_path / "D" / "project"
    staging = restored_project / "work" / "backups" / "20260724T000000Z"
    config = restored_project / "config"
    catalog = restored_project / "outputs" / "open-catalog"
    staging.mkdir(parents=True)
    config.mkdir(parents=True)
    catalog.mkdir(parents=True)
    (staging / "postgres.dump").write_bytes(b"dump")
    (config / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (catalog / "catalog.jsonl").write_text("{}\n", encoding="utf-8")

    def details(path: Path) -> dict[str, object]:
        import hashlib

        content = path.read_bytes()
        return {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    manifest = {
        "schema": "research-asset-hub.backup/v2",
        "files": {
            "work/backups/20260724T000000Z/postgres.dump": details(staging / "postgres.dump"),
            "config/sources.yaml": details(config / "sources.yaml"),
            "outputs/open-catalog/catalog.jsonl": details(catalog / "catalog.jsonl"),
        },
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    layout = discover_restored_layout(tmp_path)
    validation = verify_restored_manifest(layout)
    assert validation["status"] == "verified"
    assert validation["files_checked"] == 3
    assert validation["config_files"] == 1
    assert validation["open_catalog_files"] == 1


def test_database_comparison_covers_required_operational_records() -> None:
    counts = {table: index for index, table in enumerate(CORE_TABLES)}
    fixed = {"asset_sample": [{"id": "a"}], "domain_distribution": {"agent": 1}}
    baseline = {
        "schema_revision": "0003_search_rag_integrations",
        "counts": counts,
        "fixed_queries": fixed,
        "invariants": {
            "dangling_relations": 0,
            "duplicate_logical_relations": 0,
        },
    }
    restored = json.loads(json.dumps(baseline))
    assert compare_database_snapshots(baseline, restored)["status"] == "verified"
    restored["counts"]["annotations"] = 999
    validation = compare_database_snapshots(baseline, restored)
    assert validation["status"] == "failed"
    assert validation["count_differences"]["annotations"]["restored"] == 999


def test_dry_run_writes_report_without_calling_external_systems(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    plan = _plan(project_root, "dry-run")
    report = run_restore_rehearsal(
        plan,
        snapshot_id="latest",
        dry_run=True,
    )
    assert report["status"] == "dry-run-verified"
    assert report["safety"]["active_resources_mutated"] is False
    assert plan.report_path.is_file()
    assert plan.compose_path.is_file()


def test_active_database_snapshot_falls_back_to_native_desktop_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "schema_revision": "0004_source_retirement",
        "counts": {},
        "fixed_queries": {},
        "invariants": {},
    }

    def fail_compose(_command: str) -> dict[str, object]:
        raise RuntimeError('service "postgres" is not running')

    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = (json.dumps(expected),)
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(rehearsal, "_run_json_wsl", fail_compose)
    monkeypatch.setattr(rehearsal.psycopg, "connect", connect)

    assert rehearsal._query_active_database(tmp_path) == expected
    connect.assert_called_once()
    cursor.execute.assert_called_once()
