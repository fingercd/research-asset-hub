from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain.enums import ConfirmationStatus, SourceAssetRole
from packages.domain.models import (
    Asset,
    AuditEvent,
    Base,
    OutboxEvent,
    Relation,
    RelationEvidenceModel,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRecordVersion,
    utc_now,
)
from packages.ingestion.repository_relations import reconcile_repository_relations


def _seed_binding(session: Session) -> tuple[Asset, Asset]:
    source = Source(
        alias="source-a",
        label="Source A",
        adapter="fixture",
        root_path="C:/readonly/source",
        read_only=True,
        enabled=True,
        config={},
    )
    paper = Asset(
        asset_type="paper",
        title="Paper",
        authors=[],
        analysis={},
        source_urls=[],
        domain_slugs=["domain-a"],
        tags=[],
        attributes={},
    )
    repository = Asset(
        asset_type="repository",
        title="example/repository",
        authors=[],
        analysis={},
        source_urls=["https://github.com/example/repository"],
        domain_slugs=["domain-a"],
        tags=[],
        attributes={},
    )
    session.add_all([source, paper, repository])
    session.flush()
    source_record = SourceRecord(
        source_id=source.id,
        source_record_id="paper-1",
        asset_id=paper.id,
        outcome="ready",
        raw_data={},
        source_fields={},
        warnings=[],
    )
    session.add(source_record)
    session.flush()
    version = SourceRecordVersion(
        source_record_id=source_record.id,
        adapter_version="1",
        raw_data={},
        source_fields={},
        raw_digest="a" * 64,
    )
    session.add(version)
    session.flush()
    session.add_all(
        [
            SourceRecordAsset(
                source_record_id=source_record.id,
                asset_id=paper.id,
                source_record_version_id=version.id,
                role=SourceAssetRole.PRIMARY.value,
                evidence={"field": "asset"},
            ),
            SourceRecordAsset(
                source_record_id=source_record.id,
                asset_id=repository.id,
                source_record_version_id=version.id,
                role=SourceAssetRole.REPOSITORY.value,
                evidence={
                    "repository_url": "https://github.com/example/repository"
                },
            ),
        ]
    )
    session.flush()
    return paper, repository


def test_repository_reconciliation_preview_is_read_only_and_apply_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        paper, repository = _seed_binding(session)
        session.commit()

        preview = reconcile_repository_relations(
            session,
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )

        assert preview["status"] == "preview"
        assert preview["relations_created"] == 1
        assert preview["evidence_created"] == 1
        assert session.scalars(select(Relation)).all() == []
        assert session.scalars(select(AuditEvent)).all() == []
        assert session.scalars(select(OutboxEvent)).all() == []

        applied = reconcile_repository_relations(
            session,
            apply=True,
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )
        session.commit()

        assert applied["status"] == "applied"
        relation = session.scalar(select(Relation))
        assert relation is not None
        assert relation.subject_asset_id == paper.id
        assert relation.object_asset_id == repository.id
        assert relation.confirmation_status == "confirmed"
        assert len(session.scalars(select(RelationEvidenceModel)).all()) == 1
        assert len(session.scalars(select(AuditEvent)).all()) == 1
        assert len(session.scalars(select(OutboxEvent)).all()) == 1

        relation.deleted_at = utc_now()
        session.commit()
        restored = reconcile_repository_relations(
            session,
            apply=True,
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )
        session.commit()

        assert restored["relations_updated"] == 1
        assert relation.deleted_at is None
        assert len(session.scalars(select(AuditEvent)).all()) == 2
        assert len(session.scalars(select(OutboxEvent)).all()) == 2

        repeated = reconcile_repository_relations(
            session,
            apply=True,
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )
        session.commit()

        assert repeated["relations_created"] == 0
        assert repeated["relations_reused"] == 1
        assert repeated["evidence_created"] == 0
        assert repeated["evidence_reused"] == 1
        assert len(session.scalars(select(RelationEvidenceModel)).all()) == 1
        assert len(session.scalars(select(AuditEvent)).all()) == 2
        assert len(session.scalars(select(OutboxEvent)).all()) == 2


def test_repository_reconciliation_never_revives_rejected_relation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        paper, repository = _seed_binding(session)
        rejected = Relation(
            subject_asset_id=paper.id,
            object_asset_id=repository.id,
            relation_type="implemented_by",
            source_key="canonical",
            evidence={},
            confirmation_status="rejected",
            attributes={},
        )
        session.add(rejected)
        session.commit()

        report = reconcile_repository_relations(
            session,
            apply=True,
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )
        session.commit()

        session.refresh(rejected)
        assert report["rejected_relations_skipped"] == 1
        assert rejected.confirmation_status == "rejected"
        assert session.scalars(select(RelationEvidenceModel)).all() == []
        assert session.scalars(select(AuditEvent)).all() == []
