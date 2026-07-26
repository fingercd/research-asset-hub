from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.domain.models import Asset, Base, Representation, Source
from packages.exporting import export_operational_catalog


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
        manifest = export_operational_catalog(session, output)
        assert manifest["asset_count"] == 1
        assert manifest["source_binaries_copied"] == 0
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
