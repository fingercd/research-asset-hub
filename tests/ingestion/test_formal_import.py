from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.cli.config import LoadedSource
from packages.domain.models import (
    Asset,
    Base,
    FieldClaim,
    FieldResolution,
    OutboxEvent,
    PaperProfile,
    RepositoryProfile,
    Representation,
    RepresentationMount,
    Source,
)
from packages.domain.schemas import NormalizedRecord
from packages.ingestion import formal
from packages.ingestion.reconciliation import (
    UnapprovedWeakMergeError,
    apply_weak_merge_approvals,
    load_approved_weak_merges,
    reconcile_records,
)


def _loaded_source(root: Path, source_id: str) -> LoadedSource:
    manifest = root / "manifest.json"
    if not manifest.exists():
        manifest.write_text('{"items": []}\n', encoding="utf-8")
    return LoadedSource(
        source_id=source_id,
        label=source_id,
        domain="test-domain",
        adapter="generic_manifest",
        root=root,
        manifest=manifest,
        expected_records=1,
        read_only=True,
        raw={
            "id": source_id,
            "label": source_id,
            "domain": "test-domain",
            "adapter": "generic_manifest",
            "root": str(root),
            "manifest": "manifest.json",
            "asset_roots": {"pdf": "."},
        },
    )


def _record(source_id: str, pdf_name: str) -> NormalizedRecord:
    return NormalizedRecord.model_validate(
        {
            "source_id": source_id,
            "source_record_id": "paper-1",
            "adapter_name": "test",
            "asset": {
                "title": "A Shared Paper",
                "authors": ["A. Researcher"],
                "year": 2025,
                "domain_slugs": ["test-domain"],
                "external_identifiers": [
                    {"scheme": "doi", "value": "10.1234/shared"}
                ],
            },
            "representations": [
                {
                    "representation_type": "pdf",
                    "root_key": "pdf",
                    "path_ref": pdf_name,
                },
                {
                    "representation_type": "repository",
                    "url": "https://github.com/example/shared.git",
                },
            ],
            "relations": [
                {
                    "relation_type": "implemented_by",
                    "object": {
                        "external_identifier": {
                            "scheme": "github",
                            "value": "https://github.com/example/shared",
                        }
                    },
                    "evidence": {"source": "manifest", "field": "repository"},
                    "confirmed": True,
                }
            ],
            "raw": {"id": "paper-1"},
        }
    )


def _weak_record(source_id: str, source_record_id: str) -> NormalizedRecord:
    return NormalizedRecord.model_validate(
        {
            "source_id": source_id,
            "source_record_id": source_record_id,
            "adapter_name": "test",
            "asset": {
                "title": "A Title-Only Identity",
                "authors": ["A. Researcher"],
                "year": 2026,
                "domain_slugs": ["test-domain"],
            },
            "raw": {"id": source_record_id},
        }
    )


def test_unapproved_title_year_merge_stops_before_grouping() -> None:
    records = [_weak_record("left", "paper-1"), _weak_record("right", "paper-2")]

    with pytest.raises(UnapprovedWeakMergeError) as captured:
        formal.identity_groups(records)

    assert captured.value.groups[0]["approval_status"] == "not_allowlisted"


def test_exact_title_year_allowlist_approves_group() -> None:
    records = [_weak_record("left", "paper-1"), _weak_record("right", "paper-2")]
    identity_key = (
        "title-year:a title only identity:2026"
    )

    groups, reconciliation = formal.identity_groups(
        records,
        approved_weak_merges={
            identity_key: frozenset({"left:paper-1", "right:paper-2"})
        },
    )

    assert len(groups) == 1
    assert len(reconciliation["approved_review_groups"]) == 1
    assert reconciliation["unapproved_review_groups"] == []


def test_weak_merge_approval_fails_closed_when_record_set_expands() -> None:
    records = [
        _weak_record("left", "paper-1"),
        _weak_record("right", "paper-2"),
        _weak_record("third", "paper-3"),
    ]
    identity_key = "title-year:a title only identity:2026"

    reconciliation = apply_weak_merge_approvals(
        reconcile_records(records),
        approvals={
            identity_key: frozenset({"left:paper-1", "right:paper-2"})
        },
    )

    assert reconciliation["approved_review_groups"] == []
    assert (
        reconciliation["unapproved_review_groups"][0]["approval_status"]
        == "record_set_changed"
    )


def test_malformed_weak_merge_approval_file_fails_closed(tmp_path: Path) -> None:
    approval_path = tmp_path / "approvals.yaml"
    approval_path.write_text(
        "schema_version: 1\napproved_weak_merge_groups:\n"
        "  - identity_key: title-year:paper:2026\n"
        "    records:\n"
        "      - source_id: only-one\n"
        "        source_record_id: paper\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at least two records"):
        load_approved_weak_merges(approval_path)


def test_formal_import_rejects_unapproved_merge_before_database_write(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    roots = [tmp_path / "left", tmp_path / "right"]
    for root in roots:
        root.mkdir()
    sources = [
        _loaded_source(roots[0], "left"),
        _loaded_source(roots[1], "right"),
    ]
    records = [_weak_record("left", "paper-1"), _weak_record("right", "paper-2")]
    monkeypatch.setattr(formal, "_load_all_records", lambda _: records)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(UnapprovedWeakMergeError):
            formal.run_formal_import(
                session,
                sources,
                known_domain_slugs={"test-domain"},
            )
        assert session.scalars(select(Source)).all() == []


def test_formal_import_rejects_unregistered_domain_before_database_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "unknown"
    root.mkdir()
    source = _loaded_source(root, "unknown")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="taxonomy.yaml"):
            formal.run_formal_import(
                session,
                [source],
                known_domain_slugs={"another-domain"},
            )
        assert session.scalars(select(Source)).all() == []


def test_formal_import_rejects_normalized_unregistered_domain_before_database_write(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "custom-adapter"
    root.mkdir()
    source = _loaded_source(root, "custom-adapter")
    record = _record("custom-adapter", "paper.pdf")
    record.asset.domain_slugs = ["adapter-injected-domain"]
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(
            formal.FormalImportValidationError,
            match="Adapter output contains domains",
        ):
            formal.run_formal_import(
                session,
                [source],
                known_domain_slugs={"test-domain"},
            )
        assert session.scalars(select(Source)).all() == []


def test_formal_import_rejects_expected_record_count_mismatch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "count-mismatch"
    root.mkdir()
    source = _loaded_source(root, "count-mismatch")
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(
            formal.FormalImportValidationError,
            match="expected 1, got 0",
        ):
            formal.run_formal_import(
                session,
                [source],
                known_domain_slugs={"test-domain"},
            )
        assert session.scalars(select(Source)).all() == []


def test_formal_import_freezes_manifest_after_first_commit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "frozen"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    source = _loaded_source(root, "frozen")
    record = _record("frozen", "paper.pdf")
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        assert first["schema_version"] == 2
        source_model = session.scalar(select(Source).where(Source.alias == "frozen"))
        assert source_model is not None
        assert source_model.config["_frozen_manifest_sha256"]
        assert source_model.config["_frozen_source_config_sha256"]

        source.manifest.write_text('{"items": [{"id": "changed"}]}\n', encoding="utf-8")
        with pytest.raises(
            formal.FrozenSourceChangedError,
            match="Frozen manifest changed",
        ):
            formal.run_formal_import(
                session,
                [source],
                known_domain_slugs={"test-domain"},
            )
        session.rollback()


def test_formal_import_freezes_source_configuration_after_first_commit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "frozen-config"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    source = _loaded_source(root, "frozen-config")
    record = _record("frozen-config", "paper.pdf")
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()

        changed = replace(
            source,
            label="changed label",
            raw={**source.raw, "label": "changed label"},
        )
        with pytest.raises(
            formal.FrozenSourceChangedError,
            match="Frozen source configuration changed",
        ):
            formal.run_formal_import(
                session,
                [changed],
                known_domain_slugs={"test-domain"},
            )
        session.rollback()


def test_formal_import_is_idempotent_and_materializes_repository(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    roots = [tmp_path / "left", tmp_path / "right"]
    for index, root in enumerate(roots):
        root.mkdir()
        (root / f"paper-{index}.pdf").write_bytes(b"%PDF-1.4\n")
    sources = [
        _loaded_source(roots[0], "left"),
        _loaded_source(roots[1], "right"),
    ]
    records = [_record("left", "paper-0.pdf"), _record("right", "paper-1.pdf")]
    monkeypatch.setattr(formal, "_load_all_records", lambda _: records)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = formal.run_formal_import(
            session,
            sources,
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        assert first["counters"]["paper_assets_total"] == 1
        assert first["counters"]["repository_assets_total"] == 1
        assert first["counters"]["relations_created"] == 1
        assert first["counters"]["relation_evidence_created"] == 2
        assert formal.database_counts(session) == {
            "assets": 2,
            "papers": 1,
            "repositories": 1,
            "source_records": 2,
            "representations": 4,
            "missing_representations": 0,
            "relations": 1,
            "implemented_by": 1,
            "field_claims": 8,
        }
        outbox_count_after_first_import = len(
            session.scalars(select(OutboxEvent)).all()
        )
        assert outbox_count_after_first_import == 2
        derived_representation = session.scalar(
            select(Representation).where(
                Representation.representation_type == "pdf"
            )
        )
        assert derived_representation is not None
        derived_representation.attributes = {
            **dict(derived_representation.attributes or {}),
            "sandbox_policy": "trusted_local",
            "sandbox_policy_source": "static-html-audit/v1",
            "sandbox_policy_content_sha256": "a" * 64,
        }
        session.commit()

        second = formal.run_formal_import(
            session,
            sources,
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        assert second["counters"]["paper_assets_created"] == 0
        assert second["counters"]["repository_assets_created"] == 0
        assert second["counters"]["representations_created"] == 0
        assert second["counters"]["relations_created"] == 0
        assert second["counters"]["relation_evidence_created"] == 0
        assert (
            len(session.scalars(select(OutboxEvent)).all())
            == outbox_count_after_first_import
        )
        session.refresh(derived_representation)
        assert derived_representation.attributes["sandbox_policy"] == "trusted_local"
        assert (
            derived_representation.attributes["sandbox_policy_content_sha256"]
            == "a" * 64
        )


def test_shared_repository_provenance_does_not_emit_repeat_outbox(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "shared-source"
    root.mkdir()
    (root / "paper-1.pdf").write_bytes(b"%PDF-1.4\none")
    (root / "paper-2.pdf").write_bytes(b"%PDF-1.4\ntwo")
    source = _loaded_source(root, "shared-source")
    source = replace(
        source,
        expected_records=2,
        raw={**source.raw, "expected_records": 2},
    )
    first_record = _record("shared-source", "paper-1.pdf")
    first_record.asset.external_identifiers = []
    second_record = _record("shared-source", "paper-2.pdf").model_copy(deep=True)
    second_record.source_record_id = "paper-2"
    second_record.raw = {"id": "paper-2"}
    second_record.asset.title = "A Different Paper Sharing The Repository"
    second_record.asset.external_identifiers = []
    records = [first_record, second_record]
    monkeypatch.setattr(formal, "_load_all_records", lambda _: records)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        first_outbox_count = len(session.scalars(select(OutboxEvent)).all())
        assert first_outbox_count == 3

        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        assert len(session.scalars(select(OutboxEvent)).all()) == first_outbox_count


def test_repository_url_normalization_keeps_meaningful_subpath() -> None:
    assert (
        formal.canonical_repository_url(
            "HTTPS://GitHub.com/google-research/google-research/tree/master/saycan/"
        )
        == "https://github.com/google-research/google-research/tree/master/saycan"
    )


def test_formal_import_reapplies_active_field_override(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "override"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    source = _loaded_source(root, "override")
    record = _record("override", "paper.pdf")
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        asset = session.scalar(select(Asset).where(Asset.asset_type == "paper"))
        assert asset is not None
        resolution = session.scalar(
            select(FieldResolution).where(
                FieldResolution.entity_id == asset.id,
                FieldResolution.field_path == "/title",
            )
        )
        assert resolution is not None
        resolution.resolution_kind = "override"
        resolution.selected_claim_id = None
        resolution.override_value = "My durable title"
        asset.title = "My durable title"
        session.commit()

        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()
        session.refresh(asset)
        assert asset.title == "My durable title"
        session.refresh(resolution)
        assert resolution.resolution_kind == "override"
        assert resolution.override_value == "My durable title"


def test_container_source_path_is_persisted_as_windows_path(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_CONTAINER_WORKSPACE_ROOT", "/workspace")
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", "D:\\Research")
    assert (
        formal._persisted_source_path(Path("/workspace/domain/paper.pdf"))
        == "D:\\Research\\domain\\paper.pdf"
    )


def test_non_paper_asset_does_not_receive_paper_profile(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "html-document"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    source = _loaded_source(root, "html-document")
    record = _record("html-document", "paper.pdf")
    record.asset.asset_type = "html_document"
    record.representations = record.representations[:1]
    record.relations = []
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        report = formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()

        asset = session.scalar(select(Asset))
        assert asset is not None
        assert asset.asset_type == "html_document"
        assert session.get(PaperProfile, asset.id) is None
        assert report["counters"]["paper_assets_created"] == 0
        assert report["counters"]["paper_assets_total"] == 0


def test_formal_import_uses_adapter_declared_field_pointer(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "field-pointer"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    source = _loaded_source(root, "field-pointer")
    record = _record("field-pointer", "paper.pdf")
    record.source_path = "config/intake/field-pointer/snapshot.json"
    record.source_fields = {
        "field_pointers": {
            "title": {
                "input_path": "library/catalog/papers.json",
                "input_sha256": "a" * 64,
                "json_pointer": "/papers/0/title",
            }
        }
    }
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()

        claim = session.scalar(
            select(FieldClaim).where(FieldClaim.field_path == "/title")
        )
        assert claim is not None
        assert (
            claim.source_pointer
            == "library/catalog/papers.json#/papers/0/title"
        )


def test_reused_repository_unions_domains_and_preserves_bundle_contract(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    sources: list[LoadedSource] = []
    records: list[NormalizedRecord] = []
    for index, (source_id, domain) in enumerate(
        (("source-a", "domain-a"), ("source-b", "domain-b"))
    ):
        root = tmp_path / source_id
        (root / "code/repo/docs").mkdir(parents=True)
        (root / f"paper-{index}.pdf").write_bytes(b"%PDF-1.4\n")
        (root / "code/repo/README.md").write_text("# Repo\n", encoding="utf-8")
        (root / "code/repo/docs/index.md").write_text("# Docs\n", encoding="utf-8")
        loaded = _loaded_source(root, source_id)
        sources.append(
            replace(
                loaded,
                domain=domain,
                raw={**loaded.raw, "domain": domain},
            )
        )
        record = _record(source_id, f"paper-{index}.pdf")
        record.asset.domain_slugs = [domain]
        repository = record.representations[1]
        repository.path_ref = "code/repo"
        repository.materialization_kind = "bundle"
        repository.entrypoint = "README.md"
        repository.revision = "a" * 40
        repository.tree_digest = "b" * 64
        repository.metadata = {
            "license": "MIT",
            "status": "downloaded",
            "source_label": source_id,
        }
        repository.mounts = [
            {
                "mount_path": "/docs",
                "source_id": source_id,
                "root_key": "source",
                "path_ref": "code/repo/docs",
                "read_only": True,
                "priority": 10,
            }
        ]
        records.append(record)
    monkeypatch.setattr(formal, "_load_all_records", lambda _: records)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(
            session,
            sources,
            known_domain_slugs={"domain-a", "domain-b"},
        )
        session.commit()

        repository_asset = session.scalar(
            select(Asset).where(Asset.asset_type == "repository")
        )
        assert repository_asset is not None
        assert repository_asset.domain_slugs == ["domain-a", "domain-b"]
        profile = session.get(RepositoryProfile, repository_asset.id)
        assert profile is not None
        assert set(profile.attributes["source_metadata"]) == {
            "source-a:paper-1",
            "source-b:paper-1",
        }
        representations = session.scalars(
            select(Representation).where(
                Representation.asset_id == repository_asset.id
            )
        ).all()
        assert len(representations) == 2
        assert {item.materialization_kind for item in representations} == {"bundle"}
        assert {item.entrypoint for item in representations} == {"README.md"}
        assert {item.revision for item in representations} == {"a" * 40}
        assert {item.tree_digest for item in representations} == {"b" * 64}
        mounts = session.scalars(
            select(RepresentationMount).where(
                RepresentationMount.representation_id.in_(
                    [item.id for item in representations]
                )
            )
        ).all()
        assert len(mounts) == 4
        assert {mount.mount_path for mount in mounts} == {"/", "/docs"}
