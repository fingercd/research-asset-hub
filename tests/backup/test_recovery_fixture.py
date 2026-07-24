from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from packages.domain.models import (
    Annotation,
    AnnotationRevision,
    Asset,
    AuditEvent,
    Base,
    Integration,
    OutboxEvent,
    ReadingState,
    Relation,
    SyncConflict,
    Tombstone,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from seed_recovery_fixture import (  # type: ignore[import-not-found]  # noqa: E402
    ENTITY_IDS,
    EXPECTED_COUNTS,
    FIXTURE_ACTOR,
    FIXTURE_KEY,
    MUTATIONS,
    seed_recovery_fixture,
    verify_recovery_fixture,
)


def _engine() -> Engine:
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_seed_is_non_user_soft_deleted_and_idempotent() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = seed_recovery_fixture(session)
        session.commit()
        first_database_total = int(
            session.scalar(select(func.count()).select_from(AuditEvent)) or 0
        )

        second = seed_recovery_fixture(session)
        session.commit()
        second_database_total = int(
            session.scalar(select(func.count()).select_from(AuditEvent)) or 0
        )

        assert first["status"] == "verified"
        assert first["created_total"] == sum(EXPECTED_COUNTS.values())
        assert first["updated_total"] == 0
        assert second["status"] == "verified"
        assert second["created_total"] == 0
        assert second["updated_total"] == 0
        assert second_database_total == first_database_total == len(MUTATIONS)

        assets = session.scalars(
            select(Asset).where(
                Asset.id.in_((ENTITY_IDS["asset_a"], ENTITY_IDS["asset_b"]))
            )
        ).all()
        assert len(assets) == 2
        assert all(asset.deleted_at is not None for asset in assets)
        assert all(asset.attributes["contains_user_content"] is False for asset in assets)
        assert (
            session.scalar(
                select(func.count())
                .select_from(Asset)
                .where(
                    Asset.id.in_((ENTITY_IDS["asset_a"], ENTITY_IDS["asset_b"])),
                    Asset.deleted_at.is_(None),
                )
            )
            == 0
        )

        reading_state = session.get(ReadingState, ENTITY_IDS["reading_state"])
        annotation = session.get(Annotation, ENTITY_IDS["annotation"])
        relation = session.get(Relation, ENTITY_IDS["relation"])
        integration = session.get(Integration, ENTITY_IDS["integration"])
        conflict = session.get(SyncConflict, ENTITY_IDS["sync_conflict"])
        tombstone = session.get(Tombstone, ENTITY_IDS["tombstone"])

        assert reading_state is not None and reading_state.user_id == FIXTURE_ACTOR
        assert annotation is not None and annotation.deleted_at is not None
        assert annotation.user_id == FIXTURE_ACTOR
        assert relation is not None and relation.deleted_at is not None
        assert relation.attributes == {
            "fixture_key": FIXTURE_KEY,
            "fixture_revision": 2,
            "modified": True,
        }
        assert integration is not None and not integration.enabled
        assert conflict is not None and conflict.status == "resolved"
        assert tombstone is not None and tombstone.status == "active"

        revisions = session.scalars(
            select(AnnotationRevision)
            .where(AnnotationRevision.annotation_id == ENTITY_IDS["annotation"])
            .order_by(AnnotationRevision.revision)
        ).all()
        assert [revision.revision for revision in revisions] == [1, 2]
        assert all(revision.editor == FIXTURE_ACTOR for revision in revisions)

        audit_actions = session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_id == ENTITY_IDS["relation"]
            )
        ).all()
        assert sorted(audit_actions) == ["create", "delete", "update"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.status != "published")
            )
            == 0
        )
    engine.dispose()


def test_verify_is_read_only_and_reports_missing_fixture() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        before = int(session.scalar(select(func.count()).select_from(Asset)) or 0)
        report = verify_recovery_fixture(session)
        after = int(session.scalar(select(func.count()).select_from(Asset)) or 0)

        assert report["status"] == "incomplete"
        assert report["counts"]["assets"] == 0
        assert report["stable_ids"]["asset_a"] == str(ENTITY_IDS["asset_a"])
        assert before == after == 0
    engine.dispose()
