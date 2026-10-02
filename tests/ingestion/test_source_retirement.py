from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from packages.domain.enums import BackupStatus
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    RepresentationMount,
    Source,
    SourceRecord,
    SourceRetirement,
    SourceRetirementMountMap,
    SourceRetirementRecordMap,
    SourceRetirementRepresentationMap,
    SourceRetirementRollback,
    SourceRetirementSuccessor,
)
from packages.ingestion import source_retirement as source_retirement_module
from packages.ingestion.source_retirement import (
    SignedSourceRetirementPreview,
    SourceRetirementDriftError,
    SourceRetirementPreview,
    SourceRetirementSignatureError,
    SourceRetirementValidationError,
    active_retirement_exists,
    apply_source_retirement,
    apply_source_retirement_batch,
    canonical_payload_bytes,
    is_source_retired,
    preview_source_retirement,
    preview_source_retirement_rollback,
    rollback_source_retirement,
)

FROZEN_MANIFEST = "a" * 64
FROZEN_CONFIG = "b" * 64
CONTENT_SHA256 = "c" * 64


class Ed25519Verifier:
    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self.public_key = public_key

    def __call__(
        self,
        *,
        payload: bytes,
        signature: bytes,
        key_id: str,
        algorithm: str,
    ) -> bool:
        assert key_id == "test-key"
        assert algorithm == "ed25519"
        self.public_key.verify(signature, payload)
        return True


def _source(alias: str) -> Source:
    return Source(
        alias=alias,
        label=alias,
        adapter="fixture",
        root_path=f"C:/readonly/{alias}",
        manifest_path="manifest.json",
        read_only=True,
        enabled=True,
        config={
            "_frozen_manifest_sha256": FROZEN_MANIFEST,
            "_frozen_source_config_sha256": FROZEN_CONFIG,
        },
    )


def _asset(title: str = "Paper") -> Asset:
    return Asset(
        asset_type="paper",
        title=title,
        authors=[],
        analysis={},
        source_urls=[],
        domain_slugs=["test"],
        tags=[],
        attributes={},
    )


def _seed(session: Session, suffix: str = "") -> dict[str, Any]:
    retired_source = _source(f"retired{suffix}")
    successor_source = _source(f"successor{suffix}")
    asset = _asset(f"Paper{suffix}")
    backup = BackupRun(
        status=BackupStatus.VERIFIED.value,
        backup_kind="full",
        destination_label=f"before-source-retirement{suffix}",
        counters={},
    )
    session.add_all([retired_source, successor_source, asset, backup])
    session.flush()

    retired_record = SourceRecord(
        source_id=retired_source.id,
        source_record_id=f"paper-old{suffix}",
        asset_id=asset.id,
        outcome="ready",
        raw_data={"title": "Paper"},
        source_fields={"title": "title"},
        normalized_digest="d" * 64,
        warnings=[],
    )
    successor_record = SourceRecord(
        source_id=successor_source.id,
        source_record_id=f"paper-new{suffix}",
        asset_id=asset.id,
        outcome="ready",
        raw_data={"title": "Paper"},
        source_fields={"title": "title"},
        normalized_digest="d" * 64,
        warnings=[],
    )
    session.add_all([retired_record, successor_record])
    session.flush()

    retired_representation = Representation(
        asset_id=asset.id,
        source_id=retired_source.id,
        source_record_id=retired_record.id,
        representation_type="pdf",
        role="original",
        materialization_kind="file",
        availability="available",
        root_key="pdf",
        path_ref="paper.pdf",
        sha256=CONTENT_SHA256,
        mime_type="application/pdf",
        size_bytes=100,
        attributes={},
    )
    successor_representation = Representation(
        asset_id=asset.id,
        source_id=successor_source.id,
        source_record_id=successor_record.id,
        representation_type="pdf",
        role="original",
        materialization_kind="file",
        availability="available",
        root_key="pdf",
        path_ref="paper.pdf",
        sha256=CONTENT_SHA256,
        mime_type="application/pdf",
        size_bytes=100,
        attributes={},
    )
    session.add_all([retired_representation, successor_representation])
    session.flush()

    retired_mount = RepresentationMount(
        representation_id=retired_representation.id,
        mount_path="/paper.pdf",
        source_id=retired_source.id,
        root_key="pdf",
        path_ref="paper.pdf",
        read_only=True,
    )
    successor_mount = RepresentationMount(
        representation_id=successor_representation.id,
        mount_path="/paper.pdf",
        source_id=successor_source.id,
        root_key="pdf",
        path_ref="paper.pdf",
        read_only=True,
    )
    session.add_all([retired_mount, successor_mount])
    session.commit()
    return {
        "retired_source": retired_source,
        "successor_source": successor_source,
        "asset": asset,
        "backup": backup,
        "retired_record": retired_record,
        "successor_record": successor_record,
        "retired_representation": retired_representation,
        "successor_representation": successor_representation,
        "retired_mount": retired_mount,
        "successor_mount": successor_mount,
    }


def _preview(
    session: Session, seeded: dict[str, Any]
) -> SourceRetirementPreview:
    return preview_source_retirement(
        session,
        source_id=seeded["retired_source"].id,
        backup_id=seeded["backup"].id,
        record_mappings={
            seeded["retired_record"].id: seeded["successor_record"].id
        },
        representation_mappings={
            seeded["retired_representation"].id: seeded[
                "successor_representation"
            ].id
        },
        mount_mappings={seeded["retired_mount"].id: seeded["successor_mount"].id},
        requested_by="retirement-test",
        reason="Core-domain copy has passed opening and integrity checks.",
    )


def _sign(private_key: Ed25519PrivateKey, payload: dict[str, Any]) -> bytes:
    return private_key.sign(canonical_payload_bytes(payload))


def _signed_preview(
    private_key: Ed25519PrivateKey,
    preview: SourceRetirementPreview,
) -> SignedSourceRetirementPreview:
    return SignedSourceRetirementPreview(
        preview=preview,
        signature=_sign(private_key, preview.payload),
        signer_key_id="test-key",
    )


def _required_source(session: Session, source_id: Any) -> Source:
    source = session.get(Source, source_id)
    assert source is not None
    return source


def test_preview_apply_and_rollback_are_complete_signed_and_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        seeded = _seed(session)
        preview = _preview(session, seeded)
        assert preview.ready
        assert preview.as_dict()["counts"] == {
            "records": 1,
            "representations": 1,
            "mounts": 1,
            "successor_sources": 1,
        }
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 0
        session.rollback()

        applied = apply_source_retirement(
            session,
            preview=preview,
            signature=_sign(private_key, preview.payload),
            signer_key_id="test-key",
            signature_verifier=verifier,
        )
        assert applied["status"] == "applied"
        assert not _required_source(session, seeded["retired_source"].id).enabled
        assert is_source_retired(session, seeded["retired_source"].id)
        actively_served_representation_ids = set(
            session.scalars(
                select(Representation.id).where(
                    ~active_retirement_exists(Representation.source_id)
                )
            )
        )
        assert seeded["retired_representation"].id not in actively_served_representation_ids
        assert seeded["successor_representation"].id in actively_served_representation_ids
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 1
        assert session.scalar(
            select(func.count()).select_from(SourceRetirementRecordMap)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(SourceRetirementRepresentationMap)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(SourceRetirementMountMap)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(SourceRetirementSuccessor)
        ) == 1
        session.rollback()

        repeated = apply_source_retirement(
            session,
            preview=preview,
            signature=_sign(private_key, preview.payload),
            signer_key_id="test-key",
            signature_verifier=verifier,
        )
        assert repeated["status"] == "unchanged"
        assert repeated["idempotent"]

        rollback_payload = preview_source_retirement_rollback(
            session,
            retirement_id=applied["retirement_id"],
            reason="Recovery drill completed.",
            requested_by="retirement-test",
        )
        session.rollback()
        rolled_back = rollback_source_retirement(
            session,
            payload=rollback_payload,
            signature=_sign(private_key, rollback_payload),
            signer_key_id="test-key",
            signature_verifier=verifier,
        )
        assert rolled_back["status"] == "rolled_back"
        assert _required_source(session, seeded["retired_source"].id).enabled
        assert not is_source_retired(session, seeded["retired_source"].id)
        assert session.scalar(
            select(func.count()).select_from(SourceRetirementRollback)
        ) == 1
        session.rollback()

        repeated_rollback = rollback_source_retirement(
            session,
            payload=rollback_payload,
            signature=_sign(private_key, rollback_payload),
            signer_key_id="test-key",
            signature_verifier=verifier,
        )
        assert repeated_rollback["status"] == "unchanged"
        assert repeated_rollback["idempotent"]
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 1
        assert session.scalar(
            select(func.count()).select_from(SourceRetirementRollback)
        ) == 1


def test_preview_rejects_incomplete_mapping_and_different_asset() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seeded = _seed(session)
        incomplete = preview_source_retirement(
            session,
            source_id=seeded["retired_source"].id,
            backup_id=seeded["backup"].id,
            record_mappings={},
            representation_mappings={},
            mount_mappings={},
            requested_by="retirement-test",
        )
        assert not incomplete.ready
        assert {item.code for item in incomplete.issues} >= {
            "incomplete_record_mapping",
            "incomplete_representation_mapping",
            "incomplete_mount_mapping",
        }

        other_asset = _asset("Different")
        session.add(other_asset)
        session.flush()
        seeded["successor_record"].asset_id = other_asset.id
        seeded["successor_representation"].asset_id = other_asset.id
        session.commit()
        mismatch = _preview(session, seeded)
        assert not mismatch.ready
        assert {item.code for item in mismatch.issues} >= {
            "record_asset_mismatch",
            "representation_asset_mismatch",
        }


def test_apply_fails_closed_on_drift_or_invalid_signature() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        seeded = _seed(session)
        preview = _preview(session, seeded)
        session.rollback()

        with pytest.raises(SourceRetirementSignatureError):
            apply_source_retirement(
                session,
                preview=preview,
                signature=b"not-an-ed25519-signature",
                signer_key_id="test-key",
                signature_verifier=verifier,
            )
        assert _required_source(session, seeded["retired_source"].id).enabled
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 0
        session.rollback()

        seeded["successor_representation"].sha256 = "e" * 64
        session.commit()
        with pytest.raises(SourceRetirementDriftError):
            apply_source_retirement(
                session,
                preview=preview,
                signature=_sign(private_key, preview.payload),
                signer_key_id="test-key",
                signature_verifier=verifier,
            )
        assert _required_source(session, seeded["retired_source"].id).enabled
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 0


def test_apply_rejects_invalid_preview_before_any_mutation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        seeded = _seed(session)
        preview = preview_source_retirement(
            session,
            source_id=seeded["retired_source"].id,
            backup_id=seeded["backup"].id,
            record_mappings={},
            representation_mappings={},
            mount_mappings={},
            requested_by="retirement-test",
        )
        session.rollback()
        with pytest.raises(SourceRetirementValidationError):
            apply_source_retirement(
                session,
                preview=preview,
                signature=_sign(private_key, preview.payload),
                signer_key_id="test-key",
                signature_verifier=verifier,
            )
        assert _required_source(session, seeded["retired_source"].id).enabled
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 0


def test_batch_apply_is_atomic_and_idempotent_for_multiple_sources() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        first = _seed(session, "-first")
        second = _seed(session, "-second")
        previews = [_preview(session, first), _preview(session, second)]
        signed = [_signed_preview(private_key, item) for item in previews]
        session.rollback()

        applied = apply_source_retirement_batch(
            session,
            signed_previews=signed,
            signature_verifier=verifier,
        )
        assert applied["status"] == "applied"
        assert applied["count"] == 2
        assert not _required_source(session, first["retired_source"].id).enabled
        assert not _required_source(session, second["retired_source"].id).enabled
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 2
        session.rollback()

        repeated = apply_source_retirement_batch(
            session,
            signed_previews=signed,
            signature_verifier=verifier,
        )
        assert repeated["status"] == "unchanged"
        assert repeated["idempotent"]
        assert len(repeated["items"]) == 2
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 2


def test_batch_applies_seven_sources_in_one_transaction() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        seeded = [_seed(session, f"-{index}") for index in range(7)]
        signed = [
            _signed_preview(private_key, _preview(session, item)) for item in seeded
        ]
        session.rollback()
        report = apply_source_retirement_batch(
            session,
            signed_previews=signed,
            signature_verifier=verifier,
        )
        assert report["status"] == "applied"
        assert report["count"] == 7
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 7
        assert session.scalar(
            select(func.count()).select_from(Source).where(Source.enabled.is_(False))
        ) == 7


def test_batch_rolls_back_when_second_append_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        first = _seed(session, "-first")
        second = _seed(session, "-second")
        signed = [
            _signed_preview(private_key, _preview(session, first)),
            _signed_preview(private_key, _preview(session, second)),
        ]
        session.rollback()

        original_append = source_retirement_module._append_source_retirement
        calls = 0

        def fail_second_append(*args: Any, **kwargs: Any) -> SourceRetirement:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second append failure")
            return original_append(*args, **kwargs)

        monkeypatch.setattr(
            source_retirement_module,
            "_append_source_retirement",
            fail_second_append,
        )
        with pytest.raises(RuntimeError, match="injected second append failure"):
            apply_source_retirement_batch(
                session,
                signed_previews=signed,
                signature_verifier=verifier,
            )
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 0
        assert _required_source(session, first["retired_source"].id).enabled
        assert _required_source(session, second["retired_source"].id).enabled


def test_batch_rejects_duplicate_source_and_partially_applied_state() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        first = _seed(session, "-first")
        second = _seed(session, "-second")
        first_signed = _signed_preview(private_key, _preview(session, first))
        second_signed = _signed_preview(private_key, _preview(session, second))
        session.rollback()

        with pytest.raises(SourceRetirementValidationError, match="same source"):
            apply_source_retirement_batch(
                session,
                signed_previews=[first_signed, first_signed],
                signature_verifier=verifier,
            )
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 0
        session.rollback()

        apply_source_retirement_batch(
            session,
            signed_previews=[first_signed],
            signature_verifier=verifier,
        )
        with pytest.raises(SourceRetirementValidationError, match="partially applied"):
            apply_source_retirement_batch(
                session,
                signed_previews=[first_signed, second_signed],
                signature_verifier=verifier,
            )
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 1
        assert not _required_source(session, first["retired_source"].id).enabled
        assert _required_source(session, second["retired_source"].id).enabled


def test_concurrent_sqlite_batches_serialize_to_applied_then_unchanged(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "retirement-concurrency.sqlite3"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()
    verifier = Ed25519Verifier(private_key.public_key())
    with Session(engine) as session:
        first = _seed(session, "-first")
        second = _seed(session, "-second")
        signed = [
            _signed_preview(private_key, _preview(session, first)),
            _signed_preview(private_key, _preview(session, second)),
        ]
        session.rollback()

    barrier = Barrier(2)

    def apply_concurrently() -> str:
        with Session(engine) as concurrent_session:
            barrier.wait(timeout=5)
            report = apply_source_retirement_batch(
                concurrent_session,
                signed_previews=signed,
                signature_verifier=verifier,
            )
            return str(report["status"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _: apply_concurrently(), range(2)))
    assert statuses == ["applied", "unchanged"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SourceRetirement)) == 2
        assert session.scalar(
            select(func.count()).select_from(Source).where(Source.enabled.is_(False))
        ) == 2
