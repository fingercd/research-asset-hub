from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.cli.main import (
    _batch_preview_report,
    _external_successor_groups,
    _previews_from_external_successor,
    _snippet_safe_tree_integrity,
    main,
)
from packages.domain.enums import BackupStatus
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    Source,
    SourceRecord,
)
from packages.ingestion import safe_tree_policy
from packages.ingestion import source_retirement as source_retirement_module
from packages.ingestion.source_retirement import (
    SourceRetirementDriftError,
    SourceRetirementPreview,
    apply_source_retirement,
    canonical_payload_bytes,
    canonical_payload_digest,
)
from tools.external_asset_copy import copy_assets as external_copy_policy


def _source_preview(source_id: UUID, backup_id: UUID) -> SourceRetirementPreview:
    payload = {
        "schema_version": 1,
        "plan_id": str(UUID(int=source_id.int + 100)),
        "source_id": str(source_id),
        "source_alias": f"source-{source_id.int}",
        "backup_id": str(backup_id),
        "backup_state_digest": "a" * 64,
        "requested_by": "cli-test",
        "reason": "test",
        "frozen_manifest_sha256": "b" * 64,
        "frozen_source_config_sha256": "c" * 64,
        "record_set_digest": "d" * 64,
        "representation_set_digest": "e" * 64,
        "mount_set_digest": "f" * 64,
        "collection_digest": "1" * 64,
        "successors": [],
        "record_mappings": [],
        "representation_mappings": [],
        "mount_mappings": [],
    }
    return SourceRetirementPreview(
        payload=payload,
        payload_digest=canonical_payload_digest(payload),
        issues=(),
        warnings=(),
    )


@pytest.mark.parametrize("batch_record", [False, True])
def test_cli_applies_atomic_multi_source_retirement_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    batch_record: bool,
) -> None:
    backup_id = UUID(int=900)
    previews = [_source_preview(UUID(int=1), backup_id), _source_preview(UUID(int=2), backup_id)]
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(previews[0].as_dict()), encoding="utf-8")
    second.write_text(json.dumps(previews[1].as_dict()), encoding="utf-8")
    batch = _batch_preview_report(previews)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch), encoding="utf-8")

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
    monkeypatch.setenv("TEST_PRIVATE_KEY", base64.b64encode(private_raw).decode())
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    registry_path = tmp_path / "signers.yaml"
    registry_path.write_text(
        "schema_version: 1\nkeys:\n"
        "  - id: operator\n"
        "    algorithm: ed25519\n"
        f"    public_key: base64:{base64.b64encode(public_raw).decode()}\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_apply(
        session: Any,
        *,
        signed_previews: list[Any],
        signature_verifier: Any,
    ) -> dict[str, Any]:
        calls.append(len(signed_previews))
        for signed in signed_previews:
            assert signature_verifier(
                payload=canonical_payload_bytes(signed.preview.payload),
                signature=signed.signature,
                key_id=signed.signer_key_id,
                algorithm=signed.signature_algorithm,
            )
        return {
            "status": "applied",
            "idempotent": False,
            "count": len(signed_previews),
            "items": [
                {
                    "status": "applied",
                    "source_id": signed.preview.payload["source_id"],
                }
                for signed in signed_previews
            ],
        }

    monkeypatch.setattr(
        "packages.ingestion.source_retirement.apply_source_retirement_batch",
        fake_apply,
    )

    preview_arguments = (
        ["--preview-file", str(batch_path)]
        if batch_record
        else [
            "--preview-file",
            str(first),
            "--preview-file",
            str(second),
        ]
    )

    result = main(
        [
            "source-retirement",
            "apply",
            *preview_arguments,
            "--expect-preview",
            str(batch["batch_digest"]),
            "--signer-key-id",
            "operator",
            "--signer-registry",
            str(registry_path),
            "--private-key-env",
            "TEST_PRIVATE_KEY",
            "--json",
        ]
    )

    assert result == 0
    assert calls == [2]
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == "applied"
    assert report["signed_preview_count"] == 2
    assert report["batch_digest"] == batch["batch_digest"]


def test_cli_rejects_wrong_expected_batch_digest_before_loading_private_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup_id = UUID(int=901)
    batch = _batch_preview_report(
        [_source_preview(UUID(int=3), backup_id), _source_preview(UUID(int=4), backup_id)]
    )
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch), encoding="utf-8")

    result = main(
        [
            "source-retirement",
            "apply",
            "--preview-file",
            str(batch_path),
            "--expect-preview",
            "0" * 64,
            "--signer-key-id",
            "operator",
            "--signer-registry",
            str(tmp_path / "missing-registry.yaml"),
            "--private-key-env",
            "MISSING_PRIVATE_KEY",
            "--json",
        ]
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error_type"] == "ValueError"
    assert "canonical batch preview digest" in report["error"]


def test_batch_preview_requires_one_backup() -> None:
    with pytest.raises(ValueError, match="same backup id"):
        _batch_preview_report(
            [
                _source_preview(UUID(int=5), UUID(int=902)),
                _source_preview(UUID(int=6), UUID(int=903)),
            ]
        )


def test_external_successor_snippet_schema_groups_seven_sources() -> None:
    old_aliases = [f"old-{index}" for index in range(7)]
    new_aliases = [f"new-{index}" for index in range(7)]
    record_mappings = [
        {
            "old": {"source_id": old, "source_record_id": "paper"},
            "new": {"source_id": new, "source_record_id": "paper"},
        }
        for old, new in zip(old_aliases, new_aliases, strict=True)
    ]
    representation_mappings = [
        {
            "old": {
                "source_id": old,
                "source_record_id": "paper",
                "type": "pdf",
                "path": "paper.pdf",
            },
            "new": {
                "source_id": new,
                "source_record_id": "paper",
                "type": "pdf",
                "path": "paper.pdf",
            },
        }
        for old, new in zip(old_aliases, new_aliases, strict=True)
    ]
    snippet = {
        "schema_version": 1,
        "report_kind": "external_successor_source_registration_snippet",
        "record_mappings": record_mappings,
        "representation_mappings": representation_mappings,
        "retirement_preview": {
            "old_source_ids": old_aliases,
            "successor_source_ids": new_aliases,
            "mapping_count": 7,
            "representation_mapping_count": 7,
            "formal_reference_coverage_complete": True,
            "uncovered_formal_references": [],
        },
    }

    groups = _external_successor_groups(snippet)

    assert len(groups) == 7
    assert {group["retired_source"] for group in groups} == set(old_aliases)
    assert {group["successor_source"] for group in groups} == set(new_aliases)


def test_external_successor_snippet_resolves_database_uuid_mappings() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        old_source = Source(
            alias="old",
            label="old",
            adapter="fixture",
            root_path="C:/old",
            manifest_path="manifest.json",
            read_only=True,
            enabled=True,
            config={
                "_frozen_manifest_sha256": "a" * 64,
                "_frozen_source_config_sha256": "b" * 64,
            },
        )
        new_source = Source(
            alias="new",
            label="new",
            adapter="fixture",
            root_path="D:/core/new",
            manifest_path="manifest.json",
            read_only=True,
            enabled=True,
            config={
                "_frozen_manifest_sha256": "c" * 64,
                "_frozen_source_config_sha256": "d" * 64,
            },
        )
        asset = Asset(
            asset_type="paper",
            title="Paper",
            authors=[],
            analysis={},
            source_urls=[],
            domain_slugs=["test"],
            tags=[],
            attributes={},
        )
        backup = BackupRun(
            status=BackupStatus.VERIFIED.value,
            backup_kind="full",
            destination_label="before-batch",
            counters={},
        )
        session.add_all([old_source, new_source, asset, backup])
        session.flush()
        old_record = SourceRecord(
            source_id=old_source.id,
            source_record_id="paper",
            asset_id=asset.id,
            outcome="ready",
            raw_data={},
            source_fields={},
            normalized_digest="e" * 64,
            warnings=[],
        )
        new_record = SourceRecord(
            source_id=new_source.id,
            source_record_id="paper",
            asset_id=asset.id,
            outcome="ready",
            raw_data={},
            source_fields={},
            normalized_digest="e" * 64,
            warnings=[],
        )
        session.add_all([old_record, new_record])
        session.flush()
        old_representation = Representation(
            asset_id=asset.id,
            source_id=old_source.id,
            source_record_id=old_record.id,
            representation_type="pdf",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="pdf",
            path_ref="paper.pdf",
            sha256="f" * 64,
            attributes={},
        )
        new_representation = Representation(
            asset_id=asset.id,
            source_id=new_source.id,
            source_record_id=new_record.id,
            representation_type="pdf",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="pdf",
            path_ref="paper.pdf",
            sha256="f" * 64,
            attributes={},
        )
        session.add_all([old_representation, new_representation])
        session.commit()
        snippet = {
            "schema_version": 1,
            "report_kind": "external_successor_source_registration_snippet",
            "record_mappings": [
                {
                    "old": {"source_id": "old", "source_record_id": "paper"},
                    "new": {"source_id": "new", "source_record_id": "paper"},
                }
            ],
            "representation_mappings": [
                {
                    "old": {
                        "source_id": "old",
                        "source_record_id": "paper",
                        "type": "pdf",
                        "asset_root_key": "pdf",
                        "path": "paper.pdf",
                    },
                    "new": {
                        "source_id": "new",
                        "source_record_id": "paper",
                        "type": "pdf",
                        "asset_root_key": "pdf",
                        "path": "paper.pdf",
                    },
                }
            ],
            "retirement_preview": {
                "old_source_ids": ["old"],
                "successor_source_ids": ["new"],
                "mapping_count": 1,
                "representation_mapping_count": 1,
                "formal_reference_coverage_complete": True,
                "uncovered_formal_references": [],
            },
        }

        previews = _previews_from_external_successor(
            session,
            document=snippet,
            backup_id=str(backup.id),
            requested_by="cli-test",
            reason="core successor verified",
        )

        assert len(previews) == 1
        assert previews[0].ready
        assert previews[0].payload["record_mappings"][0][
            "retired_source_record_id"
        ] == str(old_record.id)
        assert previews[0].payload["representation_mappings"][0][
            "successor_representation_id"
        ] == str(new_representation.id)
        assert previews[0].payload["successors"][0]["alias"] == "new"


def _seed_external_directory_successor(
    session: Session,
    tmp_path: Path,
) -> dict[str, Any]:
    old_assets = tmp_path / "old-assets"
    new_assets = tmp_path / "new-assets"
    old_repo = old_assets / "paper-code"
    new_repo = new_assets / "paper-code"
    for directory in (old_repo, new_repo):
        (directory / "src").mkdir(parents=True)
        (directory / "src" / "main.py").write_text("print('safe')\n", encoding="utf-8")
        (directory / ".env").write_text("TOKEN=must-not-be-read\n", encoding="utf-8")
        (directory / "client.pem").write_text(
            "certificate-must-not-be-read\n", encoding="utf-8"
        )
        (directory / "weights").mkdir()
        (directory / "weights" / "model.bin").write_bytes(b"must-not-be-read")

    old_source = Source(
        alias="old-directory-source",
        label="old-directory-source",
        adapter="fixture",
        root_path=str(tmp_path / "old-source-config"),
        manifest_path="manifest.json",
        read_only=True,
        enabled=True,
        config={
            "asset_roots": {"repository": str(old_assets)},
            "_frozen_manifest_sha256": "a" * 64,
            "_frozen_source_config_sha256": "b" * 64,
        },
    )
    new_source = Source(
        alias="new-directory-source",
        label="new-directory-source",
        adapter="fixture",
        root_path=str(tmp_path / "new-source-config"),
        manifest_path="manifest.json",
        read_only=True,
        enabled=True,
        config={
            "asset_roots": {"repository": str(new_assets)},
            "_frozen_manifest_sha256": "c" * 64,
            "_frozen_source_config_sha256": "d" * 64,
        },
    )
    asset = Asset(
        asset_type="paper",
        title="Directory Paper",
        authors=[],
        analysis={},
        source_urls=[],
        domain_slugs=["test"],
        tags=[],
        attributes={},
    )
    backup = BackupRun(
        status=BackupStatus.VERIFIED.value,
        backup_kind="full",
        destination_label="before-directory-retirement",
        counters={},
    )
    session.add_all([old_source, new_source, asset, backup])
    session.flush()
    old_record = SourceRecord(
        source_id=old_source.id,
        source_record_id="paper-code",
        asset_id=asset.id,
        outcome="ready",
        raw_data={},
        source_fields={},
        normalized_digest="e" * 64,
        warnings=[],
    )
    new_record = SourceRecord(
        source_id=new_source.id,
        source_record_id="paper-code",
        asset_id=asset.id,
        outcome="ready",
        raw_data={},
        source_fields={},
        normalized_digest="e" * 64,
        warnings=[],
    )
    session.add_all([old_record, new_record])
    session.flush()
    old_representation = Representation(
        asset_id=asset.id,
        source_id=old_source.id,
        source_record_id=old_record.id,
        representation_type="repository",
        role="official",
        materialization_kind="directory",
        availability="available",
        root_key="repository",
        path_ref="paper-code",
        tree_digest="1" * 64,
        attributes={},
    )
    new_representation = Representation(
        asset_id=asset.id,
        source_id=new_source.id,
        source_record_id=new_record.id,
        representation_type="repository",
        role="official",
        materialization_kind="directory",
        availability="available",
        root_key="repository",
        path_ref="paper-code",
        tree_digest="2" * 64,
        attributes={},
    )
    session.add_all([old_representation, new_representation])
    session.commit()

    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    safe_files = external_copy_policy._scan_directory(
        old_repo, old_repo, skipped, errors
    )
    assert not errors
    summary = {
        "tree_sha256": external_copy_policy._content_tree_digest(safe_files),
        "file_count": len(safe_files),
        "aggregate_size_bytes": sum(item["size_bytes"] for item in safe_files),
    }
    snippet = {
        "schema_version": 1,
        "report_kind": "external_successor_source_registration_snippet",
        "record_mappings": [
            {
                "old": {
                    "source_id": old_source.alias,
                    "source_record_id": "paper-code",
                },
                "new": {
                    "source_id": new_source.alias,
                    "source_record_id": "paper-code",
                },
            }
        ],
        "representation_mappings": [
            {
                "old": {
                    "source_id": old_source.alias,
                    "source_record_id": "paper-code",
                    "type": "code",
                    "asset_root_key": "repository",
                    "path": "paper-code",
                },
                "new": {
                    "source_id": new_source.alias,
                    "source_record_id": "paper-code",
                    "type": "code",
                    "asset_root_key": "repository",
                    "path": "paper-code",
                    "integrity": summary,
                },
            }
        ],
        "retirement_preview": {
            "old_source_ids": [old_source.alias],
            "successor_source_ids": [new_source.alias],
            "mapping_count": 1,
            "representation_mapping_count": 1,
            "formal_reference_coverage_complete": True,
            "uncovered_formal_references": [],
        },
    }
    return {
        "backup": backup,
        "snippet": snippet,
        "summary": summary,
        "old_repo": old_repo,
        "new_repo": new_repo,
        "old_representation": old_representation,
    }


def _external_directory_preview(session: Session, seeded: dict[str, Any]) -> Any:
    previews = _previews_from_external_successor(
        session,
        document=seeded["snippet"],
        backup_id=str(seeded["backup"].id),
        requested_by="safe-tree-test",
        reason="safe external copy verified",
    )
    assert len(previews) == 1
    return previews[0]


def test_external_successor_safe_tree_is_rescanned_and_signed_with_policy_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seeded = _seed_external_directory_successor(session, tmp_path)
        opened: list[str] = []
        original_hash = safe_tree_policy.sha256_file

        def guarded_hash(path: Path) -> tuple[str, int]:
            assert path.name not in {".env", "client.pem", "model.bin"}
            opened.append(path.name)
            return original_hash(path)

        monkeypatch.setattr(safe_tree_policy, "sha256_file", guarded_hash)
        preview = _external_directory_preview(session, seeded)

        assert preview.ready
        evidence = preview.payload["representation_mappings"][0]["content_evidence"]
        assert evidence["kind"] == "safe_tree_sha256"
        assert evidence["value"] == seeded["summary"]["tree_sha256"]
        assert evidence["file_count"] == seeded["summary"]["file_count"]
        assert evidence["aggregate_size_bytes"] == seeded["summary"][
            "aggregate_size_bytes"
        ]
        assert evidence["policy_version"] == "external-asset-copy-safe-tree-v1"
        assert len(evidence["policy_sha256"]) == 64
        assert evidence["retired_summary"] == seeded["summary"]
        assert evidence["successor_summary"] == seeded["summary"]
        assert set(opened) == {"main.py"}

        regenerated = source_retirement_module._regenerate_preview(
            session, preview.payload
        )
        assert regenerated.ready
        assert regenerated.payload_digest == preview.payload_digest
        assert regenerated.payload == preview.payload


def test_external_successor_safe_tree_apply_rechecks_filesystem_drift(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    private_key = Ed25519PrivateKey.generate()

    def verifier(
        *, payload: bytes, signature: bytes, key_id: str, algorithm: str
    ) -> bool:
        assert key_id == "safe-tree-test"
        assert algorithm == "ed25519"
        private_key.public_key().verify(signature, payload)
        return True

    with Session(engine) as session:
        seeded = _seed_external_directory_successor(session, tmp_path)
        preview = _external_directory_preview(session, seeded)
        assert preview.ready
        session.rollback()
        (seeded["new_repo"] / "src" / "main.py").write_text(
            "print('drifted')\n", encoding="utf-8"
        )

        with pytest.raises(SourceRetirementDriftError):
            apply_source_retirement(
                session,
                preview=preview,
                signature=private_key.sign(canonical_payload_bytes(preview.payload)),
                signer_key_id="safe-tree-test",
                signature_verifier=verifier,
            )


@pytest.mark.parametrize(
    "integrity",
    [
        {"tree_sha256": "a" * 64, "file_count": 1},
        {
            "tree_sha256": "A" * 64,
            "file_count": 1,
            "aggregate_size_bytes": 1,
        },
        {
            "tree_sha256": "a" * 64,
            "file_count": True,
            "aggregate_size_bytes": 1,
        },
    ],
)
def test_external_successor_safe_tree_requires_strict_complete_declaration(
    integrity: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="directory integrity"):
        _snippet_safe_tree_integrity({"integrity": integrity})


@pytest.mark.parametrize(
    "mutation",
    ["forged_summary", "target_missing_file", "source_extra_safe_file"],
)
def test_external_successor_safe_tree_rejects_forgery_or_file_set_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seeded = _seed_external_directory_successor(session, tmp_path)
        if mutation == "forged_summary":
            seeded["snippet"]["representation_mappings"][0]["new"]["integrity"][
                "tree_sha256"
            ] = "f" * 64
        elif mutation == "target_missing_file":
            (seeded["new_repo"] / "src" / "main.py").unlink()
        else:
            (seeded["old_repo"] / "src" / "extra.py").write_text(
                "print('extra')\n", encoding="utf-8"
            )

        preview = _external_directory_preview(session, seeded)

        assert not preview.ready
        assert "representation_safe_tree_mismatch" in {
            issue.code for issue in preview.issues
        }


def test_external_successor_safe_tree_rejects_reparse_point(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unsupported")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seeded = _seed_external_directory_successor(session, tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "outside.py").write_text("secret\n", encoding="utf-8")
        try:
            os.symlink(outside, seeded["old_repo"] / "linked", target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted")

        preview = _external_directory_preview(session, seeded)

        assert not preview.ready
        assert "representation_safe_tree_invalid" in {
            issue.code for issue in preview.issues
        }


def test_external_successor_safe_tree_treats_reparse_skip_as_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seeded = _seed_external_directory_successor(session, tmp_path)
        original_scan = safe_tree_policy.scan_directory
        calls = 0

        def scan_with_reparse(
            directory: Path,
            source_root: Path,
            skipped: list[dict[str, str]],
            errors: list[dict[str, str]],
        ) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            files = original_scan(directory, source_root, skipped, errors)
            if calls == 1:
                skipped.append(
                    {
                        "source_root": str(source_root),
                        "relative_path": "linked",
                        "reason": "reparse_point",
                    }
                )
            return files

        monkeypatch.setattr(
            safe_tree_policy, "scan_directory", scan_with_reparse
        )
        preview = _external_directory_preview(session, seeded)

        assert not preview.ready
        assert "representation_safe_tree_invalid" in {
            issue.code for issue in preview.issues
        }


def test_directory_tree_mismatch_without_external_safe_evidence_still_fails(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seeded = _seed_external_directory_successor(session, tmp_path)
        del seeded["snippet"]["representation_mappings"][0]["new"]["integrity"]

        preview = _external_directory_preview(session, seeded)

        assert not preview.ready
        assert "representation_content_mismatch" in {
            issue.code for issue in preview.issues
        }


def test_formal_cli_bootstrap_without_lifecycle_tables_remains_fail_closed_at_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dry_report = {
        "schema_version": 1,
        "status": "passed",
        "generated_at": "2026-08-31T00:00:00+00:00",
        "active_sources": [],
        "retired_sources_verified": [],
        "historical_records_preserved": 0,
        "totals": {"normalized_records": 0},
        "reconciliation": {"unapproved_review_groups": []},
    }
    calls: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setattr("packages.cli.main.load_sources", lambda _path: [])
    monkeypatch.setattr(
        "packages.ingestion.dry_run.run_dry_run",
        lambda *args, **kwargs: dry_report,
    )

    def fake_formal_import(session: Any, sources: Any) -> dict[str, Any]:
        calls.append("formal")
        return {"schema_version": 2, "status": "passed"}

    monkeypatch.setattr(
        "packages.ingestion.formal.run_formal_import",
        fake_formal_import,
    )
    monkeypatch.setattr(
        "packages.cli.main.write_report",
        lambda report, name: tmp_path / name,
    )

    assert main(["ingest", "--all"]) == 0
    assert calls == ["formal"]
