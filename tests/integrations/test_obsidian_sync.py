from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain.models import Asset, Base, IntegrationObject, SyncSnapshot
from packages.integrations import (
    ObsidianVaultAdapter,
    preview_obsidian_assets,
    sync_obsidian_assets,
)


def test_initial_obsidian_sync_is_stable_and_id_based(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        asset = Asset(
            asset_type="paper",
            title="测试论文",
            authors=["作者"],
            year=2025,
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=["test-domain"],
            tags=["重点"],
            attributes={},
        )
        session.add(asset)
        session.commit()

        first = sync_obsidian_assets(session, tmp_path)
        session.commit()
        assert first["counters"]["created"] == 1
        adapter = ObsidianVaultAdapter(tmp_path)
        documents = adapter.scan()
        assert len(documents) == 1
        assert documents[0].asset_id == str(asset.id)

        existing_path = documents[0].path
        assert existing_path is not None
        second = sync_obsidian_assets(session, tmp_path)
        session.commit()
        assert second["counters"]["conflicts"] == 0
        assert adapter.scan()[0].path == existing_path
        assert len(session.scalars(select(IntegrationObject)).all()) == 1
        assert len(session.scalars(select(SyncSnapshot)).all()) == 2


def test_obsidian_preview_etag_invalidates_after_hub_change(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        asset = Asset(
            asset_type="paper",
            title="预览论文",
            authors=[],
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=[],
            tags=[],
            attributes={},
        )
        session.add(asset)
        session.commit()

        first = preview_obsidian_assets(session, tmp_path)
        assert first["preview_only"] is True
        assert first["counters"]["create"] == 1

        asset.title = "预览论文（已修改）"
        session.commit()
        second = preview_obsidian_assets(session, tmp_path)
        assert first["etag"] != second["etag"]
