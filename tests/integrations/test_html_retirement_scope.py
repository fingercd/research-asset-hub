from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.enums import BackupStatus
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    Source,
    SourceRecord,
)
from packages.ingestion.source_retirement import (
    apply_source_retirement,
    preview_source_retirement,
    preview_source_retirement_rollback,
    rollback_source_retirement,
)
from packages.integrations.html_audit import (
    DynamicPageCheck,
    DynamicProbeBatch,
    audit_registered_html,
)
from packages.integrations.html_policy import (
    HtmlPolicyInference,
    apply_html_policy_inferences,
)


@dataclass(frozen=True, slots=True)
class _RetirementHtmlRuntime:
    database: DatabaseManager
    registry: SourceRegistry
    retired_source_id: UUID
    retired_representation_id: UUID
    successor_representation_id: UUID
    backup_id: UUID
    record_mapping: dict[UUID, UUID]
    representation_mapping: dict[UUID, UUID]


class _SuccessfulProbe:
    def probe(
        self,
        representation_ids: Sequence[UUID],
        *,
        api_origin: str,
        timeout_seconds: float,
    ) -> DynamicProbeBatch:
        return DynamicProbeBatch(
            status="completed",
            reason=None,
            checks={
                representation_id: DynamicPageCheck(
                    representation_id=representation_id,
                    status="completed",
                    policy="static",
                )
                for representation_id in representation_ids
            },
        )


@pytest.fixture
def retirement_html_runtime(
    tmp_path: Path,
) -> Iterator[_RetirementHtmlRuntime]:
    retired_root = tmp_path / "retired"
    successor_root = tmp_path / "successor"
    retired_root.mkdir()
    successor_root.mkdir()
    payload = b"<!doctype html><title>Equivalent page</title>"
    (retired_root / "index.html").write_bytes(payload)
    (successor_root / "index.html").write_bytes(payload)
    content_sha256 = hashlib.sha256(payload).hexdigest()

    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)

    frozen = {
        "_frozen_manifest_sha256": "a" * 64,
        "_frozen_source_config_sha256": "b" * 64,
    }
    with database.session() as session:
        retired_source = Source(
            alias="retired-html",
            label="Retired HTML",
            adapter="generic_manifest",
            root_path=str(retired_root),
            config=frozen,
        )
        successor_source = Source(
            alias="successor-html",
            label="Successor HTML",
            adapter="generic_manifest",
            root_path=str(successor_root),
            config=frozen,
        )
        asset = Asset(asset_type="paper", title="HTML retirement scope")
        backup = BackupRun(
            status=BackupStatus.VERIFIED.value,
            destination_label="before-html-retirement",
        )
        session.add_all([retired_source, successor_source, asset, backup])
        session.flush()

        retired_record = SourceRecord(
            source_id=retired_source.id,
            source_record_id="retired-record",
            asset_id=asset.id,
            outcome="ready",
            raw_data={},
            source_fields={},
            normalized_digest="c" * 64,
            warnings=[],
        )
        successor_record = SourceRecord(
            source_id=successor_source.id,
            source_record_id="successor-record",
            asset_id=asset.id,
            outcome="ready",
            raw_data={},
            source_fields={},
            normalized_digest="c" * 64,
            warnings=[],
        )
        session.add_all([retired_record, successor_record])
        session.flush()

        retired_representation = Representation(
            asset_id=asset.id,
            source_id=retired_source.id,
            source_record_id=retired_record.id,
            representation_type="html",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="index.html",
            mime_type="text/html",
            sha256=content_sha256,
            size_bytes=len(payload),
        )
        successor_representation = Representation(
            asset_id=asset.id,
            source_id=successor_source.id,
            source_record_id=successor_record.id,
            representation_type="html",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="index.html",
            mime_type="text/html",
            sha256=content_sha256,
            size_bytes=len(payload),
        )
        session.add_all([retired_representation, successor_representation])
        session.flush()
        runtime = _RetirementHtmlRuntime(
            database=database,
            registry=SourceRegistry(
                {
                    (retired_source.alias, "html"): retired_root.resolve(),
                    (successor_source.alias, "html"): successor_root.resolve(),
                }
            ),
            retired_source_id=retired_source.id,
            retired_representation_id=retired_representation.id,
            successor_representation_id=successor_representation.id,
            backup_id=backup.id,
            record_mapping={retired_record.id: successor_record.id},
            representation_mapping={
                retired_representation.id: successor_representation.id
            },
        )

    yield runtime
    database.dispose()
    engine.dispose()


def _apply_retirement(runtime: _RetirementHtmlRuntime) -> UUID:
    with runtime.database.session() as session:
        preview = preview_source_retirement(
            session,
            source_id=runtime.retired_source_id,
            backup_id=runtime.backup_id,
            record_mappings=runtime.record_mapping,
            representation_mappings=runtime.representation_mapping,
            mount_mappings={},
            requested_by="html-scope-test",
            reason="Equivalent core successor is active.",
        )
        assert preview.ready
        session.rollback()
        applied = apply_source_retirement(
            session,
            preview=preview,
            signature=b"test-signature",
            signer_key_id="html-scope-test-key",
            signature_verifier=lambda **_: True,
        )
    return UUID(str(applied["retirement_id"]))


def _rollback_retirement(
    runtime: _RetirementHtmlRuntime,
    retirement_id: UUID,
) -> None:
    with runtime.database.session() as session:
        payload = preview_source_retirement_rollback(
            session,
            retirement_id=retirement_id,
            reason="Verify rollback restores HTML audit scope.",
            requested_by="html-scope-test",
        )
        session.rollback()
        result = rollback_source_retirement(
            session,
            payload=payload,
            signature=b"test-signature",
            signer_key_id="html-scope-test-key",
            signature_verifier=lambda **_: True,
        )
    assert result["status"] == "rolled_back"


def _audited_ids(runtime: _RetirementHtmlRuntime) -> set[UUID]:
    report = audit_registered_html(
        runtime.database,
        runtime.registry,
        dynamic_sample_size=2,
        dynamic_probe=_SuccessfulProbe(),
    )
    assert report.to_dict()["scope"] == "serving_active"
    assert report.to_dict()["status"] == "completed"
    return {page.registration.representation_id for page in report.pages}


def test_html_audit_excludes_retired_history_and_rollback_restores_it(
    retirement_html_runtime: _RetirementHtmlRuntime,
) -> None:
    runtime = retirement_html_runtime
    assert _audited_ids(runtime) == {
        runtime.retired_representation_id,
        runtime.successor_representation_id,
    }

    retirement_id = _apply_retirement(runtime)
    assert _audited_ids(runtime) == {runtime.successor_representation_id}

    _rollback_retirement(runtime, retirement_id)
    assert _audited_ids(runtime) == {
        runtime.retired_representation_id,
        runtime.successor_representation_id,
    }


def test_html_policy_rechecks_scope_before_mutating_stale_inferences(
    retirement_html_runtime: _RetirementHtmlRuntime,
) -> None:
    runtime = retirement_html_runtime
    stale_inferences = [
        HtmlPolicyInference(
            representation_id=runtime.retired_representation_id,
            policy="trusted_local",
            content_sha256="d" * 64,
        ),
        HtmlPolicyInference(
            representation_id=runtime.successor_representation_id,
            policy="trusted_local",
            content_sha256="d" * 64,
        ),
    ]
    retirement_id = _apply_retirement(runtime)

    with runtime.database.session() as session:
        result = apply_html_policy_inferences(session, stale_inferences)
    assert result["updated"] == 1
    assert result["retired"] == 1
    assert result["missing"] == 0

    with runtime.database.session() as session:
        retired = session.scalar(
            select(Representation).where(
                Representation.id == runtime.retired_representation_id
            )
        )
        successor = session.scalar(
            select(Representation).where(
                Representation.id == runtime.successor_representation_id
            )
        )
        assert retired is not None
        assert successor is not None
        assert "sandbox_policy" not in retired.attributes
        assert successor.attributes["sandbox_policy"] == "trusted_local"

    _rollback_retirement(runtime, retirement_id)
    with runtime.database.session() as session:
        restored = apply_html_policy_inferences(session, stale_inferences[:1])
    assert restored["updated"] == 1
    assert restored["retired"] == 0
