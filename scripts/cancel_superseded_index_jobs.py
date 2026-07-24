"""Cancel per-entity index jobs that a versioned bulk rebuild supersedes."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from apps.api.config import get_settings
from apps.api.database import DatabaseManager
from packages.domain.enums import AttemptStatus, AuditAction, JobStatus, OutboxStatus
from packages.domain.models import AuditEvent, Job, JobAttempt, OutboxEvent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-version", required=True)
    args = parser.parse_args()
    now = datetime.now(UTC)
    batch_id = uuid4()
    counters: Counter[str] = Counter()
    database = DatabaseManager(get_settings())
    with database.session() as session:
        jobs = session.scalars(
            select(Job)
            .where(
                Job.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
                Job.job_type.in_(["index_asset", "index_representation"]),
            )
            .with_for_update(skip_locked=True)
        ).all()
        running_job_ids = [job.id for job in jobs if job.status == JobStatus.RUNNING.value]
        attempts = (
            session.scalars(
                select(JobAttempt)
                .where(
                    JobAttempt.job_id.in_(running_job_ids),
                    JobAttempt.status == AttemptStatus.RUNNING.value,
                )
                .with_for_update()
            ).all()
            if running_job_ids
            else []
        )
        for attempt in attempts:
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.finished_at = now
            attempt.error = {
                "type": "BulkIndexSuperseded",
                "index_version": args.index_version,
                "batch_id": str(batch_id),
            }
        for job in jobs:
            counters[f"{job.job_type}:{job.status}"] += 1
            job.status = JobStatus.CANCELLED.value
            job.cancel_requested = True
            job.finished_at = now
            job.lease_owner = None
            job.lease_until = None
            job.error = {
                "code": "superseded_by_bulk_index",
                "detail": (
                    "A versioned bulk Meilisearch/Qdrant rebuild superseded this "
                    "per-entity index job."
                ),
                "index_version": args.index_version,
                "batch_id": str(batch_id),
            }
        summary = {
            "batch_id": str(batch_id),
            "index_version": args.index_version,
            "cancelled": dict(counters),
            "running_attempts_cancelled": len(attempts),
            "total": sum(counters.values()),
        }
        session.add(
            AuditEvent(
                entity_type="job_batch",
                entity_id=batch_id,
                action=AuditAction.UPDATE.value,
                actor="release-workflow",
                before={
                    "status": [
                        JobStatus.PENDING.value,
                        JobStatus.RUNNING.value,
                    ]
                },
                after={"status": JobStatus.CANCELLED.value, **summary},
                details={"reason": "superseded_by_bulk_index"},
            )
        )
        session.add(
            OutboxEvent(
                aggregate_type="job_batch",
                aggregate_id=batch_id,
                event_type="jobs.superseded-by-bulk-index",
                payload=summary,
                status=OutboxStatus.PENDING.value,
                dedupe_key=f"bulk-index:{args.index_version}:{batch_id}",
            )
        )
    print(summary)
    database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
