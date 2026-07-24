from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.api.config import PROJECT_ROOT, ApiSettings
from apps.api.database import DatabaseManager
from packages.domain.models import Asset, Base, Representation, Source
from scripts.export_catalog import DEFAULT_DESTINATION, build_parser, main


def _settings(database_path: Path) -> ApiSettings:
    return ApiSettings(
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
        database_require_migrations=False,
    )


def test_export_cli_default_and_binary_free_manifest(
    tmp_path: Path,
    capsys: Any,
) -> None:
    assert build_parser().parse_args([]).output == DEFAULT_DESTINATION
    assert DEFAULT_DESTINATION == PROJECT_ROOT / "outputs" / "open-catalog"

    database_path = tmp_path / "catalog.sqlite3"
    settings = _settings(database_path)
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            alias="private-source",
            label="Private Source",
            adapter="test",
            root_path="C:/private/source",
            read_only=True,
            enabled=True,
            config={},
        )
        asset = Asset(
            asset_type="paper",
            title="Catalog CLI Paper",
            authors=[],
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=["test-domain"],
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

    destination = tmp_path / "open-catalog"
    report_path = tmp_path / "catalog-report.json"
    manager = DatabaseManager(settings)
    assert (
        main(
            ["--output", str(destination), "--report", str(report_path)],
            settings=settings,
            database=manager,
        )
        == 0
    )
    manager.dispose()
    engine.dispose()

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["asset_count"] == 1
    assert manifest["source_binaries_copied"] == 0
    assert json.loads(report_path.read_text(encoding="utf-8")) == manifest
    assert json.loads(capsys.readouterr().out) == manifest
    assert not list(destination.rglob("*.pdf"))
    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in destination.rglob("*") if path.is_file()
    )
    assert "C:/private/source" not in rendered
