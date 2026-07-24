from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from packages.domain.models import Asset, Base, SyncConflict
from packages.integrations import ObsidianVaultAdapter
from scripts import sync_obsidian as sync_cli


def _settings(database_path: Path, vault_path: Path) -> ApiSettings:
    return ApiSettings(
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
        database_require_migrations=False,
        obsidian_vault_path=vault_path,
        obsidian_hub_directory="Managed/Research",
    )


def _database(tmp_path: Path) -> tuple[ApiSettings, DatabaseManager, Any]:
    database_path = tmp_path / "obsidian.sqlite3"
    vault_path = tmp_path / "Vault"
    vault_path.mkdir()
    settings = _settings(database_path, vault_path)
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    manager = DatabaseManager(settings)
    return settings, manager, engine


def _seed_asset(manager: DatabaseManager, *, title: str = "Private Paper") -> Asset:
    with manager.session() as session:
        asset = Asset(
            asset_type="paper",
            title=title,
            authors=[],
            abstract="PRIVATE_BODY_MUST_NOT_APPEAR",
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=["test-domain"],
            tags=[],
            attributes={},
        )
        session.add(asset)
        session.flush()
        asset_id = asset.id
    with manager.session() as session:
        result = session.get(Asset, asset_id)
        assert result is not None
        return result


def test_preview_only_writes_no_note_and_report_has_no_private_body(
    tmp_path: Path,
    capsys: Any,
) -> None:
    settings, manager, engine = _database(tmp_path)
    _seed_asset(manager)

    report_path = tmp_path / "sync-report.json"
    assert (
        sync_cli.main(
            ["--preview-only", "--report", str(report_path)],
            settings=settings,
            database=manager,
        )
        == 0
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["preview_only"] is True
    assert report["preview"]["counters"]["create"] == 1
    assert report["apply"] is None
    assert not list(settings.obsidian_vault_path.rglob("*.md"))
    rendered = report_path.read_text(encoding="utf-8")
    assert "PRIVATE_BODY_MUST_NOT_APPEAR" not in rendered
    assert str(settings.obsidian_vault_path) not in rendered
    assert "PRIVATE_BODY_MUST_NOT_APPEAR" not in capsys.readouterr().out

    manager.dispose()
    engine.dispose()


def test_apply_reuses_preview_session_and_rechecks_etag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, manager, engine = _database(tmp_path)
    seen_sessions: list[int] = []

    def fake_preview(session: Session, *_: Any, **__: Any) -> dict[str, Any]:
        seen_sessions.append(id(session))
        return {
            "status": "succeeded",
            "sync_run_id": "preview",
            "etag": "stable",
            "counters": {"assets_seen": 0, "conflicts": 0},
        }

    def fake_etag(session: Session, *_: Any, **__: Any) -> str:
        seen_sessions.append(id(session))
        return "stable"

    def fake_apply(session: Session, *_: Any, **__: Any) -> dict[str, Any]:
        seen_sessions.append(id(session))
        return {
            "status": "succeeded",
            "sync_run_id": "apply",
            "counters": {"assets_seen": 0, "conflicts": 0},
        }

    monkeypatch.setattr(sync_cli, "preview_obsidian_assets", fake_preview)
    monkeypatch.setattr(sync_cli, "obsidian_sync_etag", fake_etag)
    monkeypatch.setattr(sync_cli, "sync_obsidian_assets", fake_apply)

    report = sync_cli.run_sync(settings, preview_only=False, database=manager)

    assert report["status"] == "succeeded"
    assert len(set(seen_sessions)) == 1
    assert len(seen_sessions) == 3

    manager.dispose()
    engine.dispose()


def test_stale_preview_never_calls_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, manager, engine = _database(tmp_path)
    applied = False

    monkeypatch.setattr(
        sync_cli,
        "preview_obsidian_assets",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "sync_run_id": "preview",
            "etag": "before",
            "counters": {},
        },
    )
    monkeypatch.setattr(
        sync_cli,
        "obsidian_sync_etag",
        lambda *_args, **_kwargs: "after",
    )

    def fake_apply(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal applied
        applied = True
        return {}

    monkeypatch.setattr(sync_cli, "sync_obsidian_assets", fake_apply)

    with pytest.raises(sync_cli.SyncPreviewStaleError):
        sync_cli.run_sync(settings, preview_only=False, database=manager)
    assert applied is False

    manager.dispose()
    engine.dispose()


def test_concurrent_field_conflict_is_recorded_without_overwrite(
    tmp_path: Path,
) -> None:
    settings, manager, engine = _database(tmp_path)
    asset = _seed_asset(manager, title="Base title")
    asset_id = asset.id

    initial = sync_cli.run_sync(settings, preview_only=False, database=manager)
    assert initial["status"] == "succeeded"

    adapter = ObsidianVaultAdapter(
        settings.obsidian_vault_path,
        settings.obsidian_hub_directory,
    )
    document = adapter.scan()[0]
    external_fields = dict(document.fields)
    external_fields["title"] = "External title"
    adapter.write(
        asset_id=document.asset_id,
        hub_revision=document.hub_revision,
        fields=external_fields,
        note_body=document.note_body,
        existing_path=document.path,
    )
    with manager.session() as session:
        hub_asset = session.get(Asset, asset_id)
        assert hub_asset is not None
        hub_asset.title = "Hub title"

    result = sync_cli.run_sync(settings, preview_only=False, database=manager)

    assert result["status"] == "needs_review"
    assert result["preview"]["counters"]["conflicts"] == 1
    assert result["apply"]["counters"]["conflicts"] == 1
    with manager.session() as session:
        hub_asset = session.get(Asset, asset_id)
        assert hub_asset is not None
        assert hub_asset.title == "Hub title"
        conflicts = list(session.scalars(select(SyncConflict)))
        assert len(conflicts) == 1
        assert conflicts[0].hub_value == "Hub title"
        assert conflicts[0].external_value == "External title"
    assert adapter.scan()[0].fields["title"] == "External title"

    manager.dispose()
    engine.dispose()
