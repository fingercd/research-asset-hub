from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from packages.domain.enums import ActivityStatus, AttemptStatus, JobStatus
from packages.domain.models import Activity, Job, JobAttempt


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: UUID
    attempt_id: UUID
    job_type: str
    parameters: dict[str, object]
    attempt_number: int


def _finish_expired_work(
    session: Session,
    job: Job,
    *,
    current: datetime,
    cancelled: bool,
) -> None:
    error = {
        "type": "WorkerLeaseExpired",
        "detail": (
            "Cancellation completed after the owning worker lease expired."
            if cancelled
            else "The owning worker stopped before extraction completed."
        ),
    }
    attempts = session.scalars(
        select(JobAttempt)
        .where(
            JobAttempt.job_id == job.id,
            JobAttempt.status == AttemptStatus.RUNNING.value,
        )
        .with_for_update()
    ).all()
    for attempt in attempts:
        attempt.status = (
            AttemptStatus.CANCELLED.value if cancelled else AttemptStatus.ABANDONED.value
        )
        attempt.finished_at = current
        attempt.error = error
    activities = session.scalars(
        select(Activity)
        .where(Activity.job_id == job.id, Activity.status == ActivityStatus.RUNNING.value)
        .with_for_update()
    ).all()
    for activity in activities:
        activity.status = ActivityStatus.FAILED.value
        activity.finished_at = current
        activity.result = {**dict(activity.result or {}), "error": error}


def _recover_expired_cancellations(session: Session, *, current: datetime) -> None:
    jobs = session.scalars(
        select(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.cancel_requested.is_(True),
            Job.lease_until.is_not(None),
            Job.lease_until < current,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        _finish_expired_work(session, job, current=current, cancelled=True)
        job.status = JobStatus.CANCELLED.value
        job.finished_at = current
        job.heartbeat_at = current
        job.lease_owner = None
        job.lease_until = None


def claim_next_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimedJob | None:
    current = now or datetime.now(UTC)
    _recover_expired_cancellations(session, current=current)
    eligible = or_(
        and_(
            Job.status == JobStatus.PENDING.value,
            or_(Job.scheduled_at.is_(None), Job.scheduled_at <= current),
        ),
        and_(
            Job.status == JobStatus.RUNNING.value,
            Job.lease_until.is_not(None),
            Job.lease_until < current,
        ),
    )
    statement = (
        select(Job)
        .where(eligible, Job.cancel_requested.is_(False))
        .order_by(Job.priority.desc(), Job.scheduled_at.asc().nullsfirst(), Job.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = session.scalar(statement)
    if job is None:
        return None
    if job.status == JobStatus.RUNNING.value:
        _finish_expired_work(session, job, current=current, cancelled=False)
    job.attempts += 1
    job.status = JobStatus.RUNNING.value
    job.started_at = job.started_at or current
    job.heartbeat_at = current
    job.lease_owner = worker_id
    job.lease_until = current + timedelta(seconds=lease_seconds)
    job.error = None
    attempt = JobAttempt(
        job_id=job.id,
        attempt_number=job.attempts,
        worker_id=worker_id,
        status=AttemptStatus.RUNNING.value,
        heartbeat_at=current,
    )
    session.add(attempt)
    session.flush()
    return ClaimedJob(
        job_id=job.id,
        attempt_id=attempt.id,
        job_type=job.job_type,
        parameters=dict(job.parameters or {}),
        attempt_number=job.attempts,
    )


def renew_lease(
    session: Session,
    claim: ClaimedJob,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(UTC)
    job = session.get(Job, claim.job_id, with_for_update=True)
    attempt = session.get(JobAttempt, claim.attempt_id, with_for_update=True)
    if (
        job is None
        or attempt is None
        or job.status != JobStatus.RUNNING.value
        or job.lease_owner != worker_id
    ):
        return False
    # Cancellation is finalized by finish/fail. Keep the running handler's lease
    # valid until it returns so recovery cannot cancel work still owned by it.
    job.heartbeat_at = current
    job.lease_until = current + timedelta(seconds=lease_seconds)
    attempt.heartbeat_at = current
    return True


def finish_job(
    session: Session,
    claim: ClaimedJob,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(UTC)
    job = session.get(Job, claim.job_id, with_for_update=True)
    attempt = session.get(JobAttempt, claim.attempt_id, with_for_update=True)
    if job is None or attempt is None:
        raise LookupError("Claimed job or attempt no longer exists.")
    if job.lease_owner != worker_id:
        raise RuntimeError("Worker no longer owns the job lease.")
    if job.cancel_requested:
        job.status = JobStatus.CANCELLED.value
        attempt.status = AttemptStatus.CANCELLED.value
    else:
        job.status = JobStatus.SUCCEEDED.value
        job.progress = 1.0
        attempt.status = AttemptStatus.SUCCEEDED.value
    job.finished_at = current
    job.heartbeat_at = current
    job.lease_owner = None
    job.lease_until = None
    attempt.heartbeat_at = current
    attempt.finished_at = current
    return job.status


def fail_job(
    session: Session,
    claim: ClaimedJob,
    *,
    worker_id: str,
    error: Exception,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(UTC)
    job = session.get(Job, claim.job_id, with_for_update=True)
    attempt = session.get(JobAttempt, claim.attempt_id, with_for_update=True)
    if job is None or attempt is None:
        raise LookupError("Claimed job or attempt no longer exists.")
    if job.lease_owner != worker_id:
        raise RuntimeError("Worker no longer owns the job lease.")
    error_payload = {
        "type": type(error).__name__,
        "detail": " ".join(str(error).split())[:1000],
    }
    attempt.status = AttemptStatus.FAILED.value
    attempt.finished_at = current
    attempt.heartbeat_at = current
    attempt.error = error_payload
    job.error = error_payload
    job.heartbeat_at = current
    job.lease_owner = None
    job.lease_until = None
    if job.cancel_requested:
        job.status = JobStatus.CANCELLED.value
        job.finished_at = current
    elif job.attempts < job.max_attempts:
        job.status = JobStatus.PENDING.value
        job.scheduled_at = current + timedelta(seconds=min(300, 2 ** max(0, job.attempts - 1) * 5))
    else:
        job.status = JobStatus.FAILED.value
        job.finished_at = current
    return job.status
