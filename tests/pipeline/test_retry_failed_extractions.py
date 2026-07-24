from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain.models import AuditEvent, Base, Job, OutboxEvent
from packages.ingestion.job_recovery import retry_failed_extractions


def test_retry_failed_extractions_only_requeues_selected_error_types() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    batch_id = uuid4()
    now = datetime.now(UTC)
    with Session(engine) as session:
        recoverable = Job(
            job_type="extract_representation",
            parameters={},
            status="failed",
            attempts=3,
            max_attempts=3,
            error={"type": "DataError", "detail": "contains NUL"},
        )
        terminal = Job(
            job_type="extract_representation",
            parameters={},
            status="failed",
            attempts=3,
            error={"type": "RuntimeError", "detail": "no registered file"},
        )
        unrelated = Job(
            job_type="index_asset",
            parameters={},
            status="failed",
            error={"type": "DataError"},
        )
        session.add_all([recoverable, terminal, unrelated])
        session.flush()
        recoverable_id = recoverable.id
        terminal_id = terminal.id
        unrelated_id = unrelated.id

        result = retry_failed_extractions(
            session,
            error_types={"DataError"},
            now=now,
            batch_id=batch_id,
        )
        session.commit()

    assert result["retried"] == 1
    with Session(engine) as session:
        jobs = list(session.scalars(select(Job).order_by(Job.job_type, Job.id)))
        recovered = next(job for job in jobs if job.id == recoverable_id)
        assert recovered.status == "pending"
        assert recovered.max_attempts == 4
        assert recovered.error is None
        assert recovered.scheduled_at is not None
        assert recovered.scheduled_at.replace(tzinfo=UTC) == now
        assert next(job for job in jobs if job.id == terminal_id).status == "failed"
        assert next(job for job in jobs if job.id == unrelated_id).status == "failed"
        assert session.scalar(select(AuditEvent)).entity_id == batch_id
        assert session.scalar(select(OutboxEvent)).aggregate_id == batch_id

    engine.dispose()
