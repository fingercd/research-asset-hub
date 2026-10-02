from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from apps.worker.handlers import RuntimeHandlers, cancel_retired_pending_jobs
from packages.backup.release_report import collect_database_snapshot
from packages.domain.enums import BackupStatus, JobStatus
from packages.domain.models import (
    Annotation,
    Asset,
    BackupRun,
    Base,
    DocumentChunk,
    Job,
    Representation,
    Source,
    SourceRecord,
)
from packages.indexing import PostgresIndexCatalog
from packages.ingestion.source_integrity import verify_source_integrity
from packages.ingestion.source_retirement import (
    apply_source_retirement,
    preview_source_retirement,
)


def _seed_retirement(
    session: Session,
    *,
    external_root: Path,
    core_root: Path,
) -> tuple[Representation, Representation, Asset, BackupRun]:
    payload = b"same frozen paper"
    external_root.mkdir(parents=True)
    core_root.mkdir(parents=True)
    (external_root / "paper.pdf").write_bytes(payload)
    (core_root / "paper.pdf").write_bytes(payload)
    frozen = {
        "_frozen_manifest_sha256": "a" * 64,
        "_frozen_source_config_sha256": "b" * 64,
    }
    old_source = Source(
        alias="old-external",
        label="Old external",
        adapter="generic_manifest",
        root_path=str(external_root),
        config=frozen,
    )
    successor_source = Source(
        alias="core-successor",
        label="Core successor",
        adapter="generic_manifest",
        root_path=str(core_root),
        config=frozen,
    )
    asset = Asset(asset_type="paper", title="Scoped retirement")
    backup = BackupRun(
        status=BackupStatus.VERIFIED.value,
        destination_label="retirement-backup",
    )
    session.add_all([old_source, successor_source, asset, backup])
    session.flush()
    old_record = SourceRecord(
        source_id=old_source.id,
        source_record_id="old",
        asset_id=asset.id,
        outcome="ready",
        raw_data={},
        source_fields={},
        normalized_digest="c" * 64,
        warnings=[],
    )
    successor_record = SourceRecord(
        source_id=successor_source.id,
        source_record_id="successor",
        asset_id=asset.id,
        outcome="ready",
        raw_data={},
        source_fields={},
        normalized_digest="c" * 64,
        warnings=[],
    )
    session.add_all([old_record, successor_record])
    session.flush()
    digest = hashlib.sha256(payload).hexdigest()
    old_representation = Representation(
        asset_id=asset.id,
        source_id=old_source.id,
        source_record_id=old_record.id,
        representation_type="pdf",
        role="original",
        materialization_kind="file",
        availability="available",
        root_key="source",
        path_ref="paper.pdf",
        sha256=digest,
        attributes={
            "_source_size": len(payload),
            "_source_mtime_ns": (external_root / "paper.pdf").stat().st_mtime_ns,
        },
    )
    successor_representation = Representation(
        asset_id=asset.id,
        source_id=successor_source.id,
        source_record_id=successor_record.id,
        representation_type="pdf",
        role="original",
        materialization_kind="file",
        availability="available",
        root_key="source",
        path_ref="paper.pdf",
        sha256=digest,
        attributes={
            "_source_size": len(payload),
            "_source_mtime_ns": (core_root / "paper.pdf").stat().st_mtime_ns,
        },
    )
    session.add_all([old_representation, successor_representation])
    session.flush()
    session.add_all(
        [
            DocumentChunk(
                asset_id=asset.id,
                representation_id=old_representation.id,
                chunk_index=0,
                chunk_kind="document",
                text_content="retired text",
                content_hash="d" * 64,
                parser_name="test",
                parser_version="1",
            ),
            DocumentChunk(
                asset_id=asset.id,
                representation_id=successor_representation.id,
                chunk_index=0,
                chunk_kind="document",
                text_content="active text",
                content_hash="e" * 64,
                parser_name="test",
                parser_version="1",
            ),
            Annotation(
                asset_id=asset.id,
                representation_id=old_representation.id,
                body_markdown="retired annotation",
            ),
            Annotation(
                asset_id=asset.id,
                representation_id=successor_representation.id,
                body_markdown="active annotation",
            ),
        ]
    )
    session.flush()
    session.commit()
    preview = preview_source_retirement(
        session,
        source_id=old_source.id,
        backup_id=backup.id,
        record_mappings={old_record.id: successor_record.id},
        representation_mappings={
            old_representation.id: successor_representation.id
        },
        mount_mappings={},
        requested_by="pipeline-test",
    )
    assert preview.ready
    session.rollback()
    apply_source_retirement(
        session,
        preview=preview,
        signature=b"test-signature",
        signer_key_id="pipeline-test-key",
        signature_verifier=lambda **_: True,
    )
    return old_representation, successor_representation, asset, backup


def test_default_pipeline_scope_excludes_retired_history(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    external_root = tmp_path / "external"
    core_root = tmp_path / "core"
    with Session(engine) as session:
        retired, successor, _, _ = _seed_retirement(
            session,
            external_root=external_root,
            core_root=core_root,
        )
        registry = SourceRegistry(
            {
                ("old-external", "source"): external_root,
                ("core-successor", "source"): core_root,
            }
        )

        active_integrity = verify_source_integrity(session, registry).to_dict()
        historical_integrity = verify_source_integrity(
            session, registry, scope="all"
        ).to_dict()
        assert active_integrity["counts"]["representations_seen"] == 1
        assert active_integrity["population"] == {
            "registered_non_deleted": 2,
            "serving_active": 1,
            "retired_historical": 1,
        }
        assert historical_integrity["counts"]["representations_seen"] == 2

        active_catalog = PostgresIndexCatalog().load(session)
        historical_catalog = PostgresIndexCatalog().load(session, scope="all")
        assert {
            item.metadata["representation_id"]
            for item in active_catalog.keyword_documents["documents"]
        } == {str(successor.id)}
        assert {
            item.metadata["representation_id"]
            for item in historical_catalog.keyword_documents["documents"]
        } == {str(retired.id), str(successor.id)}

        active_snapshot = collect_database_snapshot(
            session,
            core_root=tmp_path,
        )
        historical_snapshot = collect_database_snapshot(
            session,
            scope="all",
            core_root=tmp_path,
        )
        assert active_snapshot["scope_counts"] == {
            "registered_non_deleted": 2,
            "serving_active": 1,
            "retired_historical": 1,
        }
        assert active_snapshot["representations"]["active"] == 1
        assert historical_snapshot["representations"]["active"] == 2
        assert active_snapshot["source_retirement"]["status"] == "passed"
    engine.dispose()


def test_worker_cancels_retired_jobs_and_queues_index_cleanup(tmp_path: Path) -> None:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    external_root = tmp_path / "external"
    core_root = tmp_path / "core"
    with database.session() as session:
        retired, _, asset, _ = _seed_retirement(
            session,
            external_root=external_root,
            core_root=core_root,
        )
        retired_id = retired.id
        asset_id = asset.id
        session.add_all(
            [
                Job(
                    job_type="extract_representation",
                    parameters={"representation_id": str(retired_id)},
                ),
                Job(
                    job_type="index_representation",
                    parameters={"representation_id": str(retired_id)},
                ),
            ]
        )

    with database.session() as session:
        assert cancel_retired_pending_jobs(session) == 2
        cancelled = list(
            session.scalars(
                select(Job).where(Job.status == JobStatus.CANCELLED.value)
            )
        )
        cleanup = session.scalar(
            select(Job).where(Job.dedupe_key == f"retirement-cleanup:asset:{asset_id}")
        )
        assert len(cancelled) == 2
        assert cleanup is not None
        assert cleanup.job_type == "index_asset"

    registry = SourceRegistry(
        {
            ("old-external", "source"): external_root,
            ("core-successor", "source"): core_root,
        }
    )
    skipped = RuntimeHandlers(database, settings, registry).extract_representation(
        {"representation_id": str(retired_id)}
    )
    assert skipped["status"] == "skipped_retired"
    engine.dispose()
