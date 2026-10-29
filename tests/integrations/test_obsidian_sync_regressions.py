from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import Asset, Base, ReadingState, SyncConflict, SyncSnapshot
from packages.integrations import ObsidianVaultAdapter, sync_obsidian_assets
from packages.integrations.obsidian import ObsidianContentChanged


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[TestClient, DatabaseManager, Path, Any]]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="g" * 64,
        obsidian_vault_path=tmp_path / "vault",
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
    )
    settings.sources_config_path.write_text(
        "schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8"
    )
    settings.taxonomy_config_path.write_text("schema_version: 1\ndomains: []\n", encoding="utf-8")
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with database.session() as session:
        asset = Asset(
            asset_type="paper",
            title="Base title",
            authors=[],
            analysis={},
            visibility="private",
            source_urls=[],
            domain_slugs=[],
            tags=[],
            attributes={},
        )
        session.add(asset)
        session.flush()
        asset_id = asset.id
        sync_obsidian_assets(session, settings.obsidian_vault_path)
    with TestClient(create_app(settings, database=database)) as client:
        yield client, database, settings.obsidian_vault_path, asset_id
    engine.dispose()


def edit_note(vault: Path, old: str, new: str) -> Path:
    document = ObsidianVaultAdapter(vault).scan()[0]
    assert document.path is not None
    text = document.path.read_text(encoding="utf-8")
    assert old in text
    document.path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return document.path


def make_conflict(database: DatabaseManager, vault: Path, asset_id: Any) -> dict[str, Any]:
    edit_note(vault, "title: Base title", "title: External title")
    with database.session() as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        asset.title = "Hub title"
        session.flush()
        result = sync_obsidian_assets(session, vault)
        assert result["counters"]["conflicts"] == 1
        item = session.scalar(select(SyncConflict))
        assert item is not None
        return {"id": str(item.id), "row_version": item.row_version}


@pytest.mark.parametrize("edit", ["title", "reading_status", "body", "outside_body"])
def test_apply_rejects_actual_note_edits_after_preview(runtime: Any, edit: str) -> None:
    client, database, vault, asset_id = runtime
    preview = client.post("/api/v1/integrations/obsidian/sync/preview")
    assert preview.status_code == 200
    changes = {
        "title": ("title: Base title", "title: Unreviewed title"),
        "reading_status": ("reading_status: unread", "reading_status: read"),
        "body": ("<!-- rah:user:start -->\n", "<!-- rah:user:start -->\nnew user note"),
        "outside_body": ("## 关联资产", "## 关联资产\nAdditional handwritten content"),
    }
    path = edit_note(vault, *changes[edit])
    edited = path.read_text(encoding="utf-8")
    result = client.post(
        "/api/v1/integrations/obsidian/sync/apply",
        json={"preview_id": preview.json()["sync_run_id"]},
        headers={"If-Match": preview.headers["ETag"]},
    )
    assert result.status_code == 412, result.text
    assert path.read_text(encoding="utf-8") == edited
    with database.session() as session:
        assert session.get(Asset, asset_id).title == "Base title"
        assert session.scalar(select(ReadingState)) is None


@pytest.mark.parametrize("existing", [False, True])
def test_apply_rejects_reading_state_changes_after_preview(runtime: Any, existing: bool) -> None:
    client, database, vault, asset_id = runtime
    if existing:
        with database.session() as session:
            session.add(ReadingState(asset_id=asset_id, user_id="local", status="reading"))
    preview = client.post("/api/v1/integrations/obsidian/sync/preview")
    with database.session() as session:
        state = session.scalar(select(ReadingState))
        if state is None:
            session.add(ReadingState(asset_id=asset_id, user_id="local", status="read"))
        else:
            state.status = "read"
    result = client.post(
        "/api/v1/integrations/obsidian/sync/apply",
        json={"preview_id": preview.json()["sync_run_id"]},
        headers={"If-Match": preview.headers["ETag"]},
    )
    assert result.status_code == 412, result.text
    assert ObsidianVaultAdapter(vault).scan()[0].fields["reading_status"] == "unread"


def test_first_external_reading_state_updates_note_and_baseline(runtime: Any) -> None:
    _, database, vault, _ = runtime
    edit_note(vault, "reading_status: unread", "reading_status: read")
    with database.session() as session:
        assert session.scalar(select(ReadingState)) is None
        result = sync_obsidian_assets(session, vault)
        assert result["counters"]["hub_updates"] == 1
        assert session.scalar(select(ReadingState)).status == "read"
        snapshot = session.scalar(select(SyncSnapshot).order_by(SyncSnapshot.created_at.desc()))
        assert snapshot.base_document["reading_status"] == "read"
    assert ObsidianVaultAdapter(vault).scan()[0].fields["reading_status"] == "read"
    with database.session() as session:
        result = sync_obsidian_assets(session, vault)
        assert result["counters"]["conflicts"] == 0
        assert result["counters"]["hub_updates"] == 0


@pytest.mark.parametrize(
    "strategy, value",
    [
        ("accept_incoming", "External title"),
        ("keep_local", "Hub title"),
        ("merge", "Merged title"),
    ],
)
def test_conflict_resolution_applies_choice_to_both_sides_and_baseline(
    runtime: Any,
    strategy: str,
    value: str,
) -> None:
    client, database, vault, asset_id = runtime
    item = make_conflict(database, vault, asset_id)
    path = edit_note(vault, "<!-- rah:user:end -->", "<!-- rah:user:end -->\nKeep this new content")
    edit_note(vault, "tags: []", "tags: [new-external-tag]")
    result = client.post(
        f"/api/v1/sync-conflicts/{item['id']}/resolve",
        json={"strategy": strategy, "resolved_value": value},
        headers={"If-Match": f'"{item["row_version"]}"'},
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "resolved"
    assert result.json()["resolved_value"] == value
    assert "Keep this new content" in path.read_text(encoding="utf-8")
    document = ObsidianVaultAdapter(vault).scan()[0]
    assert document.fields["title"] == value
    assert document.fields["tags"] == ["new-external-tag"]
    with database.session() as session:
        asset = session.get(Asset, asset_id)
        assert asset.title == value
        assert asset.tags == []
        snapshot = session.scalar(select(SyncSnapshot).order_by(SyncSnapshot.created_at.desc()))
        assert snapshot.base_document["title"] == value
        assert snapshot.base_document["tags"] == []
        sync = sync_obsidian_assets(session, vault)
        assert sync["counters"]["conflicts"] == 0
        assert asset.tags == ["new-external-tag"]


@pytest.mark.parametrize("side", ["hub", "external", "baseline"])
def test_conflict_rejects_stale_choice_without_overwriting_content(runtime: Any, side: str) -> None:
    client, database, vault, asset_id = runtime
    item = make_conflict(database, vault, asset_id)
    if side == "external":
        edit_note(vault, "title: External title", "title: New external title")
    else:
        with database.session() as session:
            if side == "hub":
                session.get(Asset, asset_id).title = "New Hub title"
            else:
                snapshot = session.scalar(select(SyncSnapshot))
                snapshot.base_document = {**snapshot.base_document, "title": "New baseline"}
    document = ObsidianVaultAdapter(vault).scan()[0]
    before = document.path.read_text(encoding="utf-8")
    result = client.post(
        f"/api/v1/sync-conflicts/{item['id']}/resolve",
        json={"strategy": "accept_incoming"},
        headers={"If-Match": f'"{item["row_version"]}"'},
    )
    assert result.status_code == 409, result.text
    assert document.path.read_text(encoding="utf-8") == before
    with database.session() as session:
        assert session.scalar(select(SyncConflict)).status == "pending"
        assert session.get(Asset, asset_id).title == (
            "New Hub title" if side == "hub" else "Hub title"
        )


def test_adapter_rejects_note_changed_between_scan_and_write(runtime: Any) -> None:
    _, _, vault, _ = runtime
    adapter = ObsidianVaultAdapter(vault)
    document = adapter.scan()[0]
    path = edit_note(vault, "title: Base title", "title: Concurrent title")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ObsidianContentChanged):
        adapter.write(
            asset_id=document.asset_id,
            hub_revision=document.hub_revision,
            fields=document.fields,
            existing_path=document.path,
            expected_content_digest=document.content_digest,
        )
    assert path.read_text(encoding="utf-8") == before


def test_resolving_one_field_keeps_other_conflicts_pending(runtime: Any) -> None:
    client, database, vault, asset_id = runtime
    edit_note(vault, "title: Base title", "title: External title")
    edit_note(vault, "tags: []", "tags: [external-tag]")
    with database.session() as session:
        asset = session.get(Asset, asset_id)
        asset.title = "Hub title"
        asset.tags = ["hub-tag"]
        session.flush()
        assert sync_obsidian_assets(session, vault)["counters"]["conflicts"] == 2
        items = {
            item.field_path: (str(item.id), item.row_version)
            for item in session.scalars(select(SyncConflict))
        }
    for field, value in [("/title", "External title"), ("/tags", ["external-tag"])]:
        item_id, version = items[field]
        result = client.post(
            f"/api/v1/sync-conflicts/{item_id}/resolve",
            json={"strategy": "accept_incoming"},
            headers={"If-Match": f'"{version}"'},
        )
        assert result.status_code == 200, result.text
        assert result.json()["resolved_value"] == value
        with database.session() as session:
            if field == "/title":
                tag_conflict = session.get(SyncConflict, UUID(items["/tags"][0]))
                assert tag_conflict.status == "pending"
    with database.session() as session:
        assert sync_obsidian_assets(session, vault)["counters"]["conflicts"] == 0
        asset = session.get(Asset, asset_id)
        assert asset.title == "External title"
        assert asset.tags == ["external-tag"]


def test_conflict_resolution_keeps_if_match_and_validation_guards(runtime: Any) -> None:
    client, database, vault, asset_id = runtime
    item = make_conflict(database, vault, asset_id)
    url = f"/api/v1/sync-conflicts/{item['id']}/resolve"
    before = ObsidianVaultAdapter(vault).scan()[0]
    assert (
        client.post(
            url, json={"strategy": "accept_incoming"}, headers={"If-Match": '"999"'}
        ).status_code
        == 412
    )
    result = client.post(
        url,
        json={"strategy": "merge", "resolved_value": ""},
        headers={"If-Match": f'"{item["row_version"]}"'},
    )
    assert result.status_code == 422
    assert ObsidianVaultAdapter(vault).scan()[0].content_digest == before.content_digest
    with database.session() as session:
        assert session.get(Asset, asset_id).title == "Hub title"
        assert session.scalar(select(SyncConflict)).status == "pending"


def test_apply_rechecks_content_loaded_after_entry_etag_validation(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.api.routes import integrations

    client, database, vault, asset_id = runtime
    preview = client.post("/api/v1/integrations/obsidian/sync/preview")
    sync = integrations.sync_obsidian_assets

    def edit_before_scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        edit_note(vault, "title: Base title", "title: Concurrent title")
        return sync(*args, **kwargs)

    monkeypatch.setattr(integrations, "sync_obsidian_assets", edit_before_scan)
    result = client.post(
        "/api/v1/integrations/obsidian/sync/apply",
        json={"preview_id": preview.json()["sync_run_id"]},
        headers={"If-Match": preview.headers["ETag"]},
    )
    assert result.status_code == 412, result.text
    with database.session() as session:
        assert session.get(Asset, asset_id).title == "Base title"
    assert ObsidianVaultAdapter(vault).scan()[0].fields["title"] == "Concurrent title"


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("strategy", ["keep_local", "merge"])
def test_failed_resolution_restores_note_and_allows_retry(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    strategy: str,
) -> None:
    from apps.api.routes import integrations

    client, database, vault, asset_id = runtime
    item = make_conflict(database, vault, asset_id)
    document = ObsidianVaultAdapter(vault).scan()[0]
    original = document.path.read_text(encoding="utf-8")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Injected resolution transaction failure")

    with monkeypatch.context() as fault:
        if failure == "audit":
            fault.setattr(integrations, "record_mutation", fail)
        else:
            fault.setattr(Session, "commit", fail)
        with pytest.raises(RuntimeError, match="Injected resolution transaction failure"):
            client.post(
                f"/api/v1/sync-conflicts/{item['id']}/resolve",
                json={"strategy": strategy, "resolved_value": "Merged title"},
                headers={"If-Match": f'"{item["row_version"]}"'},
            )
    assert document.path.read_text(encoding="utf-8") == original
    with database.session() as session:
        assert session.get(Asset, asset_id).title == "Hub title"
        assert session.scalar(select(SyncConflict)).status == "pending"
        assert len(session.scalars(select(SyncSnapshot)).all()) == 1
    retry = client.post(
        f"/api/v1/sync-conflicts/{item['id']}/resolve",
        json={"strategy": strategy, "resolved_value": "Merged title"},
        headers={"If-Match": f'"{item["row_version"]}"'},
    )
    assert retry.status_code == 200, retry.text


def test_resolution_rollback_does_not_overwrite_user_edit_after_write(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, vault, asset_id = runtime
    item = make_conflict(database, vault, asset_id)

    def edit_and_fail(*args: Any, **kwargs: Any) -> None:
        edit_note(vault, "title: Hub title", "title: Newer user title")
        raise RuntimeError("Injected commit failure after user edit")

    with monkeypatch.context() as fault:
        fault.setattr(Session, "commit", edit_and_fail)
        with pytest.raises(RuntimeError, match="Injected commit failure"):
            client.post(
                f"/api/v1/sync-conflicts/{item['id']}/resolve",
                json={"strategy": "keep_local"},
                headers={"If-Match": f'"{item["row_version"]}"'},
            )
    assert ObsidianVaultAdapter(vault).scan()[0].fields["title"] == "Newer user title"
    with database.session() as session:
        assert session.get(Asset, asset_id).title == "Hub title"
        assert session.scalar(select(SyncConflict)).status == "pending"


def test_sync_commit_failure_restores_notes_and_reading_state(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, vault, asset_id = runtime
    path = edit_note(vault, "reading_status: unread", "reading_status: read")
    original = path.read_text(encoding="utf-8")
    preview = client.post("/api/v1/integrations/obsidian/sync/preview")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Injected sync commit failure")

    with monkeypatch.context() as fault:
        fault.setattr(Session, "commit", fail)
        with pytest.raises(RuntimeError, match="Injected sync commit failure"):
            client.post(
                "/api/v1/integrations/obsidian/sync/apply",
                json={"preview_id": preview.json()["sync_run_id"]},
                headers={"If-Match": preview.headers["ETag"]},
            )
    assert path.read_text(encoding="utf-8") == original
    with database.session() as session:
        assert session.scalar(select(ReadingState)) is None


def test_preview_etag_describes_the_notes_actually_used_for_preview(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.integrations import obsidian_sync

    client, database, vault, asset_id = runtime
    get_etag = obsidian_sync.obsidian_sync_etag

    def edit_during_preview(*args: Any, **kwargs: Any) -> str:
        edit_note(vault, "title: Base title", "title: Changed during preview")
        return get_etag(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(obsidian_sync, "obsidian_sync_etag", edit_during_preview)
        preview = client.post("/api/v1/integrations/obsidian/sync/preview")
    assert preview.status_code == 200, preview.text
    result = client.post(
        "/api/v1/integrations/obsidian/sync/apply",
        json={"preview_id": preview.json()["sync_run_id"]},
        headers={"If-Match": preview.headers["ETag"]},
    )
    assert result.status_code == 412, result.text
    with database.session() as session:
        assert session.get(Asset, asset_id).title == "Base title"


def test_manual_conflict_resolution_can_explicitly_clear_nullable_field(runtime: Any) -> None:
    client, database, vault, asset_id = runtime
    edit_note(vault, "venue: null", "venue: External venue")
    with database.session() as session:
        asset = session.get(Asset, asset_id)
        asset.venue = "Hub venue"
        session.flush()
        assert sync_obsidian_assets(session, vault)["counters"]["conflicts"] == 1
        item = session.scalar(select(SyncConflict))
        item_id, version = str(item.id), item.row_version
    result = client.post(
        f"/api/v1/sync-conflicts/{item_id}/resolve",
        json={"strategy": "merge", "resolved_value": None},
        headers={"If-Match": f'"{version}"'},
    )
    assert result.status_code == 200, result.text
    assert ObsidianVaultAdapter(vault).scan()[0].fields["venue"] is None
    with database.session() as session:
        assert session.get(Asset, asset_id).venue is None
        assert sync_obsidian_assets(session, vault)["counters"]["conflicts"] == 0
