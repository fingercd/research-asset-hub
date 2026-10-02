from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRetirement,
    SourceRetirementRepresentationMap,
    SourceRetirementRollback,
)
from packages.exporting import export_operational_catalog


def _catalog_document(output: Path) -> dict[str, Any]:
    line = (output / "catalog.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _seed_retired_catalog_source(
    session: Session,
) -> tuple[Asset, Representation, Representation, SourceRetirement]:
    retired_source = Source(
        alias="desktop-retired",
        label="Retired desktop source",
        adapter="test",
        root_path="C:/Users/ExampleUser/Desktop/private-library",
        read_only=True,
        enabled=True,
        config={},
    )
    successor_source = Source(
        alias="core-successor",
        label="Core successor",
        adapter="test",
        root_path="D:/PythonProject/AI/core-library",
        read_only=True,
        enabled=True,
        config={},
    )
    asset = Asset(
        asset_type="paper",
        title="Catalog retirement scope",
        authors=[],
        analysis={},
        visibility="private",
        source_urls=[],
        domain_slugs=["catalog-retirement-test"],
        tags=[],
        attributes={},
    )
    backup = BackupRun(destination_label="catalog-retirement-test")
    session.add_all([retired_source, successor_source, asset, backup])
    session.flush()

    retired_record = SourceRecord(
        source_id=retired_source.id,
        source_record_id="desktop-record",
        asset_id=asset.id,
        outcome="ready",
        raw_data={},
        source_fields={},
        warnings=[],
    )
    successor_record = SourceRecord(
        source_id=successor_source.id,
        source_record_id="core-record",
        asset_id=asset.id,
        outcome="ready",
        raw_data={},
        source_fields={},
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
        path_ref="Desktop-only-secret.pdf",
        external_url="https://user:password@example.invalid/private.pdf?token=secret",
        sha256="a" * 64,
        attributes={"private_location": "C:/Users/ExampleUser/Desktop/private-library"},
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
        path_ref="papers/successor.pdf",
        sha256="a" * 64,
        attributes={},
    )
    session.add_all([retired_representation, successor_representation])
    session.flush()
    session.add_all(
        [
            SourceRecordAsset(
                source_record_id=retired_record.id,
                asset_id=asset.id,
                role="primary",
                evidence={},
            ),
            SourceRecordAsset(
                source_record_id=successor_record.id,
                asset_id=asset.id,
                role="primary",
                evidence={},
            ),
        ]
    )
    digest = "b" * 64
    retirement = SourceRetirement(
        source_id=retired_source.id,
        backup_id=backup.id,
        payload={"source_id": str(retired_source.id)},
        payload_digest="c" * 64,
        frozen_manifest_sha256=digest,
        frozen_source_config_sha256=digest,
        record_set_digest=digest,
        representation_set_digest=digest,
        mount_set_digest=digest,
        collection_digest=digest,
        signature_algorithm="ed25519",
        signer_key_id="catalog-test",
        signature="test-signature",
        applied_by="pytest",
    )
    session.add(retirement)
    session.flush()
    session.add(
        SourceRetirementRepresentationMap(
            retirement_id=retirement.id,
            retired_representation_id=retired_representation.id,
            successor_representation_id=successor_representation.id,
            asset_id=asset.id,
            content_digest=digest,
            retired_representation_digest=digest,
            successor_representation_digest=digest,
        )
    )
    session.commit()
    return asset, retired_representation, successor_representation, retirement


def test_open_catalog_contains_aliases_but_no_binary_copy(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            alias="sample",
            label="Sample",
            adapter="test",
            root_path="C:/private/source",
            read_only=True,
            enabled=True,
            config={},
        )
        asset = Asset(
            asset_type="paper",
            title="Paper",
            authors=[],
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=["new-domain"],
            tags=[],
            attributes={},
        )
        session.add_all([source, asset])
        session.flush()
        session.add(
            Representation(
                asset_id=asset.id,
                source_id=source.id,
                representation_type="pdf",
                role="original",
                materialization_kind="file",
                availability="available",
                root_key="pdf",
                path_ref="paper.pdf",
                sha256="a" * 64,
                attributes={},
            )
        )
        session.commit()

        output = tmp_path / "catalog"
        stale_id = "11111111-1111-1111-1111-111111111111"
        stale_canonical = output / "assets" / stale_id
        stale_domain = output / "by-domain" / "new-domain" / stale_id
        stale_canonical.mkdir(parents=True)
        stale_domain.mkdir(parents=True)
        (stale_canonical / "asset.json").write_text("{}\n", encoding="utf-8")
        (stale_domain / "asset.json").write_text("{}\n", encoding="utf-8")
        manifest = export_operational_catalog(session, output)
        assert manifest["asset_count"] == 1
        assert manifest["source_binaries_copied"] == 0
        assert manifest["stale_directories_removed"] == 2
        assert not stale_canonical.exists()
        assert not stale_domain.exists()
        payload = json.loads(
            (output / "by-domain" / "new-domain" / str(asset.id) / "asset.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["title"] == "Paper"
        representations = json.loads(
            (
                output
                / "by-domain"
                / "new-domain"
                / str(asset.id)
                / "representations.json"
            ).read_text(encoding="utf-8")
        )
        assert representations[0]["source_alias"] == "sample"
        assert "C:/private/source" not in json.dumps(representations)
        domains = json.loads((output / "domains.json").read_text(encoding="utf-8"))
        assert domains["domains"][0]["slug"] == "new-domain"
        assert domains["domains"][0]["paper_count"] == 1
        assert (
            output / "domains" / "new-domain" / "assets.jsonl"
        ).read_text(encoding="utf-8").count("\n") == 1


def test_open_catalog_exports_hidden_domain_overview_separately(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            alias="portal-source",
            label="Portal source",
            adapter="test",
            root_path="C:/private/source",
            read_only=True,
            enabled=True,
            config={},
        )
        paper = Asset(
            asset_type="paper",
            title="Paper",
            authors=[],
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=["domain-a"],
            tags=[],
            attributes={},
        )
        overview = Asset(
            asset_type="collection",
            title="Domain A overview",
            authors=[],
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=["domain-a"],
            tags=[],
            attributes={
                "kind": "domain-overview",
                "system_hidden": True,
                "domain_label": "领域 A",
                "description": "A description",
            },
        )
        session.add_all([source, paper, overview])
        session.flush()
        representation = Representation(
            asset_id=overview.id,
            source_id=source.id,
            representation_type="html",
            role="primary",
            materialization_kind="directory",
            availability="available",
            root_key="html",
            path_ref="index.html",
            entrypoint="index.html",
            sha256="b" * 64,
            attributes={},
        )
        session.add(representation)
        session.commit()

        output = tmp_path / "catalog"
        manifest = export_operational_catalog(session, output)

        assert manifest["domain_count"] == 1
        domain = json.loads(
            (output / "domains" / "domain-a" / "domain.json").read_text(
                encoding="utf-8"
            )
        )
        assert domain["label"] == "领域 A"
        assert domain["asset_count"] == 1
        assert domain["paper_count"] == 1
        assert domain["overview_asset_id"] == str(overview.id)
        assert domain["overview_representation_id"] == str(representation.id)
        overview_payload = json.loads(
            (output / "domains" / "domain-a" / "overview.json").read_text(
                encoding="utf-8"
            )
        )
        assert overview_payload["status"] == "available"
        content = (
            output / "domains" / "domain-a" / "assets.jsonl"
        ).read_text(encoding="utf-8")
        assert str(paper.id) in content
        assert str(overview.id) not in content


def test_open_catalog_excludes_retired_source_locations(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, retired, successor, _ = _seed_retired_catalog_source(session)

        output = tmp_path / "active-catalog"
        export_operational_catalog(session, output)
        document = _catalog_document(output)

        assert [item["representation_id"] for item in document["representations"]] == [
            str(successor.id)
        ]
        assert [
            item["source_alias"]
            for item in document["provenance"]["source_records"]
        ] == ["core-successor"]
        rendered = "\n".join(
            path.read_text(encoding="utf-8")
            for path in output.rglob("*")
            if path.is_file()
        )
        assert str(retired.id) not in rendered
        assert "desktop-retired" not in rendered
        assert "desktop-record" not in rendered
        assert "Desktop-only-secret.pdf" not in rendered
        assert "private-library" not in rendered
        assert "user:password" not in rendered
        assert "token=secret" not in rendered

    engine.dispose()


def test_open_catalog_rollback_restores_retired_source_locations(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, retired, successor, retirement = _seed_retired_catalog_source(session)
        backup = BackupRun(destination_label="catalog-rollback-test")
        session.add(backup)
        session.flush()
        session.add(
            SourceRetirementRollback(
                retirement_id=retirement.id,
                backup_id=backup.id,
                payload={"retirement_id": str(retirement.id)},
                payload_digest="d" * 64,
                signature_algorithm="ed25519",
                signer_key_id="catalog-test",
                signature="test-signature",
                reason="pytest rollback",
                rolled_back_by="pytest",
            )
        )
        session.commit()

        output = tmp_path / "rollback-catalog"
        export_operational_catalog(session, output)
        document = _catalog_document(output)

        assert {
            item["representation_id"] for item in document["representations"]
        } == {str(retired.id), str(successor.id)}
        assert {
            item["source_alias"]
            for item in document["provenance"]["source_records"]
        } == {"desktop-retired", "core-successor"}

    engine.dispose()
