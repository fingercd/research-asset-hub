from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.worker.lease import claim_next_job, finish_job
from packages.domain.models import Base, Job, JobAttempt


def test_api_cancelled_lease_recovery_can_be_retried(tmp_path: Path) -> None:
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
    settings.taxonomy_config_path.write_text(
        "schema_version: 1\ndomains: []\n", encoding="utf-8"
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            job = Job(job_type="noop", max_attempts=1)
            session.add(job)
            session.flush()
            job_id = job.id
            original = claim_next_job(session, worker_id="stopped", lease_seconds=60, now=now)
            assert original is not None

        with TestClient(create_app(settings, database=database)) as client:
            response = client.post(f"/api/v1/jobs/{job_id}/cancel")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "running"
            assert response.json()["cancel_requested"] is True
            assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409

            with database.session() as session:
                assert claim_next_job(
                    session,
                    worker_id="replacement",
                    lease_seconds=60,
                    now=now + timedelta(seconds=61),
                ) is None
            jobs = client.get("/api/v1/jobs").json()["items"]
            assert next(job for job in jobs if job["id"] == str(job_id))["status"] == "cancelled"
            response = client.post(f"/api/v1/jobs/{job_id}/retry")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "pending"
            assert response.json()["cancel_requested"] is False
            assert response.json()["max_attempts"] == 2
            assert response.json()["finished_at"] is None
            with database.session() as session:
                retried = claim_next_job(
                    session,
                    worker_id="replacement",
                    lease_seconds=60,
                    now=now + timedelta(seconds=62),
                )
                assert retried is not None and retried.attempt_number == 2
                assert finish_job(session, retried, worker_id="replacement") == "succeeded"
            with database.session() as session:
                attempts = session.scalars(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.attempt_number)
                ).all()
                assert [attempt.status for attempt in attempts] == ["cancelled", "succeeded"]
    finally:
        engine.dispose()
