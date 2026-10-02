from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pytest import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.cli.config import LoadedSource
from packages.domain.enums import BackupStatus
from packages.domain.models import (
    BackupRun,
    Base,
    Source,
    SourceRecord,
    SourceRetirement,
)
from packages.domain.schemas import NormalizedRecord
from packages.ingestion import formal
from packages.ingestion.dry_run import run_dry_run
from packages.ingestion.source_lifecycle_signing import (
    PublicKeyRegistry,
    load_private_key,
    sign_payload,
)


def _source(root: Path, alias: str) -> LoadedSource:
    root.mkdir()
    manifest = root / "manifest.json"
    manifest.write_text('{"items": []}\n', encoding="utf-8")
    raw = {
        "id": alias,
        "label": alias,
        "domain": "test-domain",
        "adapter": "generic_manifest",
        "root": str(root),
        "manifest": "manifest.json",
        "asset_roots": {"pdf": "."},
    }
    return LoadedSource(
        source_id=alias,
        label=alias,
        domain="test-domain",
        adapter="generic_manifest",
        root=root,
        manifest=manifest,
        expected_records=1,
        read_only=True,
        raw=raw,
    )


def _record(alias: str) -> NormalizedRecord:
    return NormalizedRecord.model_validate(
        {
            "source_id": alias,
            "source_record_id": "paper-1",
            "adapter_name": "test",
            "asset": {
                "title": f"Paper {alias}",
                "year": 2026,
                "domain_slugs": ["test-domain"],
                "external_identifiers": [
                    {"scheme": "doi", "value": f"10.1000/{alias}"}
                ],
            },
            "raw": {"id": "paper-1"},
        }
    )


def _append_retirement(session: Session, source: Source) -> None:
    backup = BackupRun(
        status=BackupStatus.VERIFIED.value,
        backup_kind="full",
        destination_label="before-test-retirement",
        counters={},
    )
    session.add(backup)
    session.flush()
    frozen_manifest = source.config["_frozen_manifest_sha256"]
    frozen_config = source.config["_frozen_source_config_sha256"]
    session.add(
        SourceRetirement(
            source_id=source.id,
            backup_id=backup.id,
            payload={"schema_version": 1},
            payload_digest="1" * 64,
            frozen_manifest_sha256=frozen_manifest,
            frozen_source_config_sha256=frozen_config,
            record_set_digest="2" * 64,
            representation_set_digest="3" * 64,
            mount_set_digest="4" * 64,
            collection_digest="5" * 64,
            signature_algorithm="ed25519",
            signer_key_id="test",
            signature="not-secret-test-signature",
            applied_by="test",
        )
    )
    source.enabled = False
    session.commit()


def test_formal_import_verifies_but_does_not_normalize_retired_source(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    retired = _source(tmp_path / "retired", "retired")
    active = _source(tmp_path / "active", "active")
    records = {"retired": _record("retired"), "active": _record("active")}
    monkeypatch.setattr(
        formal,
        "_load_all_records",
        lambda sources: [records[item.source_id] for item in sources],
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(
            session,
            [retired, active],
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        retired_model = session.scalar(select(Source).where(Source.alias == "retired"))
        assert retired_model is not None
        _append_retirement(session, retired_model)

        with pytest.raises(
            formal.FormalImportValidationError,
            match="must remain in the complete source configuration",
        ):
            formal.run_formal_import(
                session,
                [active],
                known_domain_slugs={"test-domain"},
            )
        session.rollback()

        report = formal.run_formal_import(
            session,
            [retired, active],
            known_domain_slugs={"test-domain"},
        )
        session.commit()

        assert report["active_sources"] == ["active"]
        assert report["retired_sources_verified"] == ["retired"]
        assert report["historical_records_preserved"] == 1
        session.refresh(retired_model)
        assert not retired_model.enabled
        assert len(
            session.scalars(
                select(SourceRecord).where(SourceRecord.source_id == retired_model.id)
            ).all()
        ) == 1

        retired.manifest.write_text('{"items": [{"id": "drift"}]}\n', encoding="utf-8")
        with pytest.raises(formal.FrozenSourceChangedError):
            formal.run_formal_import(
                session,
                [retired, active],
                known_domain_slugs={"test-domain"},
            )


def test_retirement_aware_dry_run_hashes_contract_and_skips_adapter(
    tmp_path: Path,
) -> None:
    retired = _source(tmp_path / "retired", "retired")
    manifest_digest = hashlib.sha256(retired.manifest.read_bytes()).hexdigest()
    config_digest = hashlib.sha256(
        json.dumps(
            retired.raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    report = run_dry_run(
        [retired],
        known_domain_slugs={"test-domain"},
        retired_source_contracts={
            "retired": {
                "manifest_sha256": manifest_digest,
                "source_config_sha256": config_digest,
                "historical_records_preserved": 7,
            }
        },
    )

    assert report["status"] == "passed"
    assert report["active_sources"] == []
    assert report["retired_sources_verified"] == ["retired"]
    assert report["historical_records_preserved"] == 7
    assert report["totals"]["normalized_records"] == 0
    assert report["totals"]["active_accounted_total"] == 0
    assert report["totals"]["historical_records_preserved"] == 7
    assert report["totals"]["accounted_total"] == 7


def test_ed25519_registry_and_explicit_environment_private_key(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("TEST_RETIREMENT_PRIVATE_KEY", base64.b64encode(private_raw).decode())
    registry_path = tmp_path / "signers.yaml"
    registry_path.write_text(
        "schema_version: 1\nkeys:\n"
        "  - id: operator\n"
        "    algorithm: ed25519\n"
        f"    public_key: base64:{base64.b64encode(public_raw).decode()}\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    loaded_private = load_private_key(private_key_env="TEST_RETIREMENT_PRIVATE_KEY")
    registry = PublicKeyRegistry.load(registry_path)
    payload = {"schema_version": 1, "backup_id": "backup"}
    signature = sign_payload(loaded_private, payload)

    from packages.ingestion.source_retirement import canonical_payload_bytes

    assert registry(
        payload=canonical_payload_bytes(payload),
        signature=signature,
        key_id="operator",
        algorithm="ed25519",
    )
    assert not registry(
        payload=canonical_payload_bytes(payload),
        signature=signature,
        key_id="unknown",
        algorithm="ed25519",
    )
