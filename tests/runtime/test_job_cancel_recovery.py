from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.worker.lease import claim_next_job, fail_job, finish_job, renew_lease
from packages.domain.models import Activity, Base, Job, JobAttempt


@pytest.fixture
def database() -> Iterator[DatabaseManager]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    yield DatabaseManager(settings, engine=engine, require_migrations=False)
    engine.dispose()


def test_expired_cancel_request_closes_work_and_claims_next_job(
    database: DatabaseManager,
) -> None:
    now = datetime.now(UTC)
    with database.session() as session:
        created_cancelled = Job(job_type="noop", priority=100, max_attempts=1)
        created_ordinary = Job(job_type="noop")
        session.add_all([created_cancelled, created_ordinary])
        session.flush()
        cancelled_id, ordinary_id = created_cancelled.id, created_ordinary.id
        claim = claim_next_job(session, worker_id="stopped", lease_seconds=60, now=now)
        assert claim is not None and claim.job_id == cancelled_id
        created_cancelled.cancel_requested = True
        session.add_all(
            [
                Activity(
                    job_id=cancelled_id,
                    activity_type="extraction",
                    tool_name="test",
                    result={"partial_chunks": 3},
                ),
                Activity(
                    job_id=cancelled_id,
                    activity_type="previous-step",
                    tool_name="test",
                    status="succeeded",
                    result={"completed": True},
                ),
            ]
        )

    # The cancelled job is still owned through the exact lease boundary.
    with database.session() as session:
        next_claim = claim_next_job(
            session,
            worker_id="replacement",
            lease_seconds=60,
            now=now + timedelta(seconds=60),
        )
        assert next_claim is not None and next_claim.job_id == ordinary_id
        cancelled = session.get(Job, cancelled_id)
        assert cancelled is not None and cancelled.status == "running"
        assert cancelled.lease_owner == "stopped"

    recovered_at = now + timedelta(seconds=61)
    with database.session() as session:
        assert claim_next_job(
            session, worker_id="replacement", lease_seconds=60, now=recovered_at
        ) is None

    with database.session() as session:
        cancelled = session.get(Job, cancelled_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested is True
        assert cancelled.attempts == 1
        assert cancelled.lease_owner is None and cancelled.lease_until is None
        assert cancelled.finished_at is not None
        attempts = session.scalars(
            select(JobAttempt).where(JobAttempt.job_id == cancelled_id)
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "cancelled"
        assert attempts[0].finished_at is not None
        assert attempts[0].error is not None
        assert attempts[0].error["type"] == "WorkerLeaseExpired"
        assert "Cancellation" in attempts[0].error["detail"]
        activities = {
            activity.activity_type: activity
            for activity in session.scalars(
                select(Activity).where(Activity.job_id == cancelled_id)
            )
        }
        assert activities["extraction"].status == "failed"
        assert activities["extraction"].finished_at is not None
        assert activities["extraction"].result["partial_chunks"] == 3
        assert "Cancellation" in activities["extraction"].result["error"]["detail"]
        assert activities["previous-step"].status == "succeeded"
        assert activities["previous-step"].result == {"completed": True}
        ordinary = session.get(Job, ordinary_id)
        assert ordinary is not None and ordinary.status == "running"

    with database.session() as session:
        assert not renew_lease(session, claim, worker_id="stopped", lease_seconds=60)
        with pytest.raises(RuntimeError, match="no longer owns"):
            finish_job(session, claim, worker_id="stopped")
    with database.session() as session:
        with pytest.raises(RuntimeError, match="no longer owns"):
            fail_job(session, claim, worker_id="stopped", error=RuntimeError("late"))


def test_cancel_request_keeps_live_worker_lease_and_rejects_other_worker(
    database: DatabaseManager,
) -> None:
    now = datetime.now(UTC)
    with database.session() as session:
        created_job = Job(job_type="noop")
        session.add(created_job)
        session.flush()
        claim = claim_next_job(session, worker_id="alive", lease_seconds=60, now=now)
        assert claim is not None
        created_job.cancel_requested = True

    with database.session() as session:
        assert not renew_lease(
            session, claim, worker_id="other", lease_seconds=60, now=now
        )
        assert renew_lease(
            session,
            claim,
            worker_id="alive",
            lease_seconds=60,
            now=now + timedelta(seconds=50),
        )
    with database.session() as session:
        assert claim_next_job(
            session,
            worker_id="replacement",
            lease_seconds=60,
            now=now + timedelta(seconds=61),
        ) is None
        job = session.get(Job, claim.job_id)
        attempt = session.get(JobAttempt, claim.attempt_id)
        assert job is not None and job.status == "running"
        assert job.lease_owner == "alive"
        assert attempt is not None and attempt.status == "running"


def test_expired_cancellation_does_not_delay_ordinary_job(
    database: DatabaseManager,
) -> None:
    now = datetime.now(UTC)
    with database.session() as session:
        created_cancelled = Job(
            job_type="noop",
            status="running",
            cancel_requested=True,
            attempts=1,
            priority=100,
            lease_owner="stopped",
            lease_until=now - timedelta(seconds=1),
        )
        ordinary = Job(job_type="noop")
        session.add_all([created_cancelled, ordinary])
        session.flush()
        cancelled_id, ordinary_id = created_cancelled.id, ordinary.id
        session.add(
            JobAttempt(job_id=cancelled_id, attempt_number=1, worker_id="stopped")
        )
    with database.session() as session:
        claim = claim_next_job(session, worker_id="replacement", lease_seconds=60, now=now)
        assert claim is not None and claim.job_id == ordinary_id
        cancelled = session.get(Job, cancelled_id)
        assert cancelled is not None and cancelled.status == "cancelled"
        assert finish_job(session, claim, worker_id="replacement", now=now) == "succeeded"


@pytest.mark.parametrize("completion", ["finish", "fail"])
def test_live_cancel_request_preserves_normal_completion(
    database: DatabaseManager, completion: str
) -> None:
    with database.session() as session:
        created_job = Job(job_type="noop")
        session.add(created_job)
        session.flush()
        claim = claim_next_job(session, worker_id="alive", lease_seconds=60)
        assert claim is not None
        created_job.cancel_requested = True
    with database.session() as session:
        if completion == "finish":
            assert finish_job(session, claim, worker_id="alive") == "cancelled"
        else:
            assert fail_job(
                session, claim, worker_id="alive", error=RuntimeError("interrupted")
            ) == "cancelled"
    with database.session() as session:
        job = session.get(Job, claim.job_id)
        attempt = session.get(JobAttempt, claim.attempt_id)
        assert job is not None and job.finished_at is not None
        assert job.lease_owner is None and job.lease_until is None
        assert job.status == "cancelled"
        assert attempt is not None and attempt.finished_at is not None
        assert attempt.status == ("cancelled" if completion == "finish" else "failed")
