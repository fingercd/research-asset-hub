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

