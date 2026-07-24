"""Auditable recovery operations for failed ingestion jobs."""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.enums import (
    ActivityStatus,
    AttemptStatus,
    AuditAction,
    JobStatus,
    OutboxStatus,
)
from packages.domain.models import (
    Activity,
    AuditEvent,
    Job,
    JobAttempt,
    OutboxEvent,
)

DEFAULT_RECOVERABLE_EXTRACTION_ERRORS = ("DataError", "OperationalError")
_GENERATION = re.compile(r"^[A-Za-z0-9._-]+$")


def retry_failed_extractions(
    session: Session,
    *,
    error_types: Collection[str] = DEFAULT_RECOVERABLE_EXTRACTION_ERRORS,
    now: datetime | None = None,
    batch_id: UUID | None = None,
) -> dict[str, Any]:
    """Requeue failed extraction jobs and record one auditable batch mutation."""

    current = now or datetime.now(UTC)
    current_batch_id = batch_id or uuid4()
    allowed = frozenset(error_types)
    jobs = session.scalars(
        select(Job)
        .where(
            Job.job_type == "extract_representation",
            Job.status == JobStatus.FAILED.value,
        )
        .with_for_update(skip_locked=True)
    ).all()
    selected = [
        job
        for job in jobs
        if isinstance(job.error, dict) and job.error.get("type") in allowed
    ]
    summary: dict[str, Any] = {
        "batch_id": str(current_batch_id),
        "error_types": sorted(allowed),
        "retried": len(selected),
        "job_ids": [str(job.id) for job in selected],
    }
    if not selected:
        return summary

    for job in selected:
        job.status = JobStatus.PENDING.value
        job.progress = 0.0
        job.max_attempts = max(job.max_attempts, job.attempts + 1)
        job.cancel_requested = False
        job.error = None
        job.finished_at = None
        job.scheduled_at = current
        job.lease_owner = None
        job.lease_until = None

    session.add(
        AuditEvent(
            entity_type="job_batch",
            entity_id=current_batch_id,
            action=AuditAction.UPDATE.value,
            actor="release-workflow",
            before={"status": JobStatus.FAILED.value, "count": len(selected)},
            after={"status": JobStatus.PENDING.value, **summary},
            details={"reason": "recoverable_extractor_fix"},
        )
    )
    session.add(
        OutboxEvent(
            aggregate_type="job_batch",
            aggregate_id=current_batch_id,
            event_type="jobs.recoverable-extractions-retried",
            payload=summary,
            status=OutboxStatus.PENDING.value,
            dedupe_key=f"retry-extractions:{current_batch_id}",
        )
    )
    return summary


def supersede_extraction_generation(
    session: Session,
    *,
    previous_generation: str,
    successor_generation: str,
    now: datetime | None = None,
    batch_id: UUID | None = None,
) -> dict[str, Any]:
    """Cancel an obsolete pending extraction generation with full provenance."""

    if not _GENERATION.fullmatch(previous_generation) or not _GENERATION.fullmatch(
        successor_generation
    ):
        raise ValueError("Extraction generation names contain unsupported characters.")
    current = now or datetime.now(UTC)
    current_batch_id = batch_id or uuid4()
    jobs = session.scalars(
        select(Job)
        .where(
            Job.job_type == "extract_representation",
            Job.status.in_(
                [JobStatus.PENDING.value, JobStatus.RUNNING.value]
            ),
            Job.dedupe_key.like(f"bootstrap:{previous_generation}:%"),
        )
        .with_for_update(skip_locked=True)
    ).all()
    summary: dict[str, Any] = {
        "batch_id": str(current_batch_id),
        "previous_generation": previous_generation,
        "successor_generation": successor_generation,
        "cancelled": len(jobs),
        "job_ids": [str(job.id) for job in jobs],
    }
    if not jobs:
        return summary

    job_ids = [job.id for job in jobs]
    attempts = session.scalars(
        select(JobAttempt)
        .where(
            JobAttempt.job_id.in_(job_ids),
            JobAttempt.status == AttemptStatus.RUNNING.value,
        )
        .with_for_update()
    ).all()
    for attempt in attempts:
        attempt.status = AttemptStatus.ABANDONED.value
        attempt.finished_at = current
        attempt.error = {
            "type": "ExtractionGenerationSuperseded",
            "detail": f"Superseded by {successor_generation}.",
        }
    activities = session.scalars(
        select(Activity)
        .where(
            Activity.job_id.in_(job_ids),
            Activity.status == ActivityStatus.RUNNING.value,
        )
        .with_for_update()
    ).all()
    for activity in activities:
        activity.status = ActivityStatus.FAILED.value
        activity.finished_at = current
        activity.result = {
            **dict(activity.result or {}),
            "error": {
                "type": "ExtractionGenerationSuperseded",
                "detail": f"Superseded by {successor_generation}.",
            },
        }
    for job in jobs:
        job.status = JobStatus.CANCELLED.value
        job.cancel_requested = True
        job.finished_at = current
        job.lease_owner = None
        job.lease_until = None
        job.error = {
            "type": "ExtractionGenerationSuperseded",
            "previous_generation": previous_generation,
            "successor_generation": successor_generation,
        }

    session.add(
        AuditEvent(
            entity_type="job_batch",
            entity_id=current_batch_id,
            action=AuditAction.UPDATE.value,
            actor="release-workflow",
            before={"generation": previous_generation},
            after={"generation": successor_generation, **summary},
            details={"reason": "parser_generation_superseded"},
        )
    )
    session.add(
        OutboxEvent(
            aggregate_type="job_batch",
            aggregate_id=current_batch_id,
            event_type="jobs.extraction-generation-superseded",
            payload=summary,
            status=OutboxStatus.PENDING.value,
            dedupe_key=f"supersede-extraction:{current_batch_id}",
        )
    )
    return summary
