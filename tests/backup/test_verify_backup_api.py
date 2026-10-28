from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.api.routes import backups
from packages.domain.models import BackupRun, Base

SNAPSHOT_ID = "abcdef12" + "0" * 56


@pytest.fixture
def backup_runtime(tmp_path: Path) -> Iterator[tuple[TestClient, DatabaseManager]]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="g" * 64,
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
    )
    settings.sources_config_path.write_text(
        "schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8"
    )
    settings.taxonomy_config_path.write_text("schema_version: 1\ndomains: []\n", encoding="utf-8")
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with TestClient(create_app(settings, database=database)) as client:
        yield client, database
    engine.dispose()


def backup_run(database, *, status="succeeded", snapshot_id=SNAPSHOT_ID):
    with database.session() as session:
        run = BackupRun(
            status=status,
            destination_label="recorded-repository",
            counters={"repository": "selected-repository", "snapshot_id": snapshot_id},
            error={"detail": "backup failed"} if status == "failed" else None,
        )
        session.add(run)
        session.flush()
        return run.id, run.row_version


def stored_run(database, backup_id):
    with database.session() as session:
        return session.get(BackupRun, backup_id)


def verification(snapshot_id):
    return {
        "status": "verified",
        "snapshot_id": snapshot_id,
        "repository_check": "verified",
        "verification_scope": "snapshot-storage-integrity",
        "data_read": "all-repository-data",
        "restore_rehearsal": "not_run",
    }


@pytest.mark.parametrize(
    ("status", "snapshot_id"),
    [("failed", None), ("failed", SNAPSHOT_ID), ("running", SNAPSHOT_ID), ("succeeded", None)],
)
def test_unsuccessful_or_snapshotless_backup_cannot_be_verified(
    backup_runtime, monkeypatch, status, snapshot_id
):
    client, database = backup_runtime
    backup_id, version = backup_run(database, status=status, snapshot_id=snapshot_id)
    monkeypatch.setattr(backups, "BackupManager", lambda **_: pytest.fail("Restic was invoked"))
    result = client.post(f"/api/v1/backups/{backup_id}/verify")
    assert result.status_code == 409
    run = stored_run(database, backup_id)
    assert run.status == status
    assert run.row_version == version
    assert run.verified_at is None
    if status == "failed":
        assert run.error == {"detail": "backup failed"}


@pytest.mark.parametrize("failure", ["snapshot missing", "corrupt pack"])
def test_snapshot_verification_failure_preserves_backup_record(
    backup_runtime, monkeypatch, failure
):
    client, database = backup_runtime
    backup_id, version = backup_run(database)

    class Manager:
        def __init__(self, *, repository):
            assert repository == Path("selected-repository")

        def verify_snapshot(self, snapshot_id):
            assert snapshot_id == SNAPSHOT_ID
            raise RuntimeError(failure)

    monkeypatch.setattr(backups, "BackupManager", Manager)
    result = client.post(f"/api/v1/backups/{backup_id}/verify")
    assert result.status_code == 503
    run = stored_run(database, backup_id)
    assert run.status == "succeeded"
    assert run.row_version == version
    assert run.verified_at is None
    assert "snapshot_verification" not in run.counters


def test_success_verifies_selected_snapshot_without_claiming_restore(backup_runtime, monkeypatch):
    client, database = backup_runtime
    backup_id, version = backup_run(database)
    calls = []

    class Manager:
        def __init__(self, *, repository):
            calls.append(repository)

        def verify_snapshot(self, snapshot_id):
            calls.append(snapshot_id)
            return verification(snapshot_id)

    monkeypatch.setattr(backups, "BackupManager", Manager)
    result = client.post(
        f"/api/v1/backups/{backup_id}/verify", headers={"If-Match": f'W/"{version}"'}
    )
    assert result.status_code == 200, result.text
    assert calls == [Path("selected-repository"), SNAPSHOT_ID]
    run = stored_run(database, backup_id)
    assert run.status == "verified"
    assert run.verified_at is not None
    assert run.counters["snapshot_verification"]["restore_rehearsal"] == "not_run"
    assert run.counters["snapshot_verification"]["data_read"] == "all-repository-data"
    assert result.headers["ETag"] == f'W/"{run.row_version}"'


def test_unknown_or_stale_backup_is_rejected_before_restic(backup_runtime, monkeypatch):
    client, database = backup_runtime
    backup_id, version = backup_run(database)
    monkeypatch.setattr(backups, "BackupManager", lambda **_: pytest.fail("Restic was invoked"))
    assert client.post(f"/api/v1/backups/{uuid4()}/verify").status_code == 404
    result = client.post(
        f"/api/v1/backups/{backup_id}/verify", headers={"If-Match": f'W/"{version + 1}"'}
    )
    assert result.status_code == 412


def test_concurrent_change_during_verification_is_not_overwritten(backup_runtime, monkeypatch):
    client, database = backup_runtime
    backup_id, _ = backup_run(database)

    class Manager:
        def __init__(self, *, repository):
            pass

        def verify_snapshot(self, snapshot_id):
            with database.session() as session:
                run = session.get(BackupRun, UUID(str(backup_id)))
                run.status = "failed"
                run.error = {"detail": "concurrent failure"}
            return verification(snapshot_id)

    monkeypatch.setattr(backups, "BackupManager", Manager)
    result = client.post(f"/api/v1/backups/{backup_id}/verify")
    assert result.status_code == 412
    run = stored_run(database, backup_id)
    assert run.status == "failed"
    assert run.error == {"detail": "concurrent failure"}
    assert run.verified_at is None
