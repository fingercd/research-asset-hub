from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

# Register the independently implemented workflow models/routes in isolated apps.
from apps.api import routes_v1  # noqa: F401
from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.api.routes import workflows  # noqa: F401
from packages.domain.models import (
    Asset,
    AuditEvent,
    Base,
    FieldClaim,
    FieldResolution,
    OutboxEvent,
    ReadingState,
    Relation,
    Representation,
)


@pytest.fixture
def runtime(tmp_path: Path):
    sources = tmp_path / "sources.yaml"
    sources.write_text("schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        "schema_version: 1\ndomains:\n  - id: agent\n    label: Agent\n", encoding="utf-8"
    )
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="g" * 64,
        sources_config_path=sources,
        taxonomy_config_path=taxonomy,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    app = create_app(settings, database=database)
    with TestClient(app) as client:
        yield client, database
    engine.dispose()


def asset(database: DatabaseManager, title: str = "Paper") -> str:
    with database.session() as session:
        item = Asset(asset_type="paper", title=title, tags=["source-tag"], domain_slugs=["agent"])
        session.add(item)
        session.flush()
        return str(item.id)


def test_collection_members_soft_deletion_and_version_condition(runtime):
    client, database = runtime
    paper = asset(database)
    collection = client.post(
        "/api/v1/collections", json={"label": "My reading", "description": "next"}
    )
    assert collection.status_code == 201, collection.text
    cid = collection.json()["id"]
    result = client.post(
        f"/api/v1/collections/{cid}/members", json={"asset_ids": [paper, str(uuid4())]}
    )
    assert [item["status"] for item in result.json()["items"]] == ["updated", "failed"]
    assert (
        client.post(f"/api/v1/collections/{cid}/members", json={"asset_ids": [paper]}).json()[
            "items"
        ][0]["status"]
        == "unchanged"
    )
    assert client.get(f"/api/v1/collections/{cid}/members").json()["items"][0]["id"] == paper
    assert client.patch(f"/api/v1/collections/{cid}", json={"label": "renamed"}).status_code == 428
    assert (
        client.patch(
            f"/api/v1/collections/{cid}", json={"label": "renamed"}, headers={"If-Match": 'W/"99"'}
        ).status_code
        == 412
    )
    edited = client.patch(
        f"/api/v1/collections/{cid}",
        json={"label": "renamed"},
        headers={"If-Match": collection.headers["etag"]},
    )
    assert edited.status_code == 200
    deleted = client.delete(
        f"/api/v1/collections/{cid}", headers={"If-Match": edited.headers["etag"]}
    )
    assert deleted.status_code == 204
    with database.session() as session:
        assert session.get(Asset, UUID(paper)).deleted_at is None
        assert session.scalar(select(func.count()).select_from(Relation)) == 1


def test_bulk_preserves_source_claim_and_partially_succeeds(runtime):
    client, database = runtime
    paper = asset(database)
    response = client.post(
        "/api/v1/assets/bulk",
        json={
            "asset_ids": [paper, str(uuid4())],
            "add_tags": ["next"],
            "remove_tags": ["source-tag"],
            "reading_status": "reading",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["succeeded"] == 1 and response.json()["failed"] == 1
    with database.session() as session:
        assert session.get(Asset, UUID(paper)).tags == ["next"]
        resolution = session.scalar(
            select(FieldResolution).where(FieldResolution.entity_id == UUID(paper))
        )
        assert resolution.field_path == "/tags" and resolution.override_value == ["next"]
        assert session.scalar(select(FieldClaim)).value_json == ["source-tag"]
        assert session.scalar(select(ReadingState)).status == "reading"
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_type == "reading_state")
        )
        assert event.payload["after"]["asset_id"] == paper
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 3
    unknown = client.post(
        "/api/v1/assets/bulk", json={"asset_ids": [paper], "add_domains": ["unknown"]}
    )
    assert unknown.status_code == 422


def test_saved_search_validates_filters_and_versions(runtime):
    client, _ = runtime
    response = client.post(
        "/api/v1/saved-searches",
        json={"name": "Next", "definition": {"q": "agents", "domain": "agent", "has_pdf": True}},
    )
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    assert response.json()["definition"]["has_pdf"] is True
    assert (
        client.post(
            "/api/v1/saved-searches",
            json={"name": "bad", "definition": {"year_from": 2026, "year_to": 2020}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/saved-searches", json={"name": "bad", "definition": {"cursor": "transient"}}
        ).status_code
        == 422
    )
    assert (
        client.patch(f"/api/v1/saved-searches/{sid}", json={"name": "renamed"}).status_code == 428
    )
    edited = client.patch(
        f"/api/v1/saved-searches/{sid}",
        json={"name": "renamed"},
        headers={"If-Match": response.headers["etag"]},
    )
    assert edited.status_code == 200
    assert (
        client.delete(
            f"/api/v1/saved-searches/{sid}", headers={"If-Match": edited.headers["etag"]}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/saved-searches").json()["total"] == 0


def test_reading_position_binds_digest_and_rejects_other_asset_pdf(runtime):
    client, database = runtime
    paper = asset(database)
    other = asset(database, "Other")
    with database.session() as session:
        pdf = Representation(
            asset_id=UUID(paper),
            representation_type="pdf",
            role="original",
            path_ref="paper.pdf",
            sha256="a" * 64,
        )
        foreign = Representation(
            asset_id=UUID(other),
            representation_type="pdf",
            role="original",
            path_ref="other.pdf",
            sha256="b" * 64,
        )
        session.add_all([pdf, foreign])
        session.flush()
        rid, foreign_id = str(pdf.id), str(foreign.id)
    assert client.get(f"/api/v1/assets/{paper}/reading-position").json() == {
        "position": None,
        "stale": False,
    }
    assert (
        client.put(
            f"/api/v1/assets/{paper}/reading-position",
            json={"representation_id": foreign_id, "page": 2},
        ).status_code
        == 422
    )
    saved = client.put(
        f"/api/v1/assets/{paper}/reading-position",
        json={"representation_id": rid, "page": 3, "source_hash": "a" * 64},
    )
    assert saved.status_code == 200 and saved.json()["stale"] is False
    assert (
        client.put(
            f"/api/v1/assets/{paper}/reading-position", json={"representation_id": rid, "page": 4}
        ).status_code
        == 428
    )
    with database.session() as session:
        session.get(Representation, UUID(rid)).sha256 = "c" * 64
    restored = client.get(f"/api/v1/assets/{paper}/reading-position")
    assert restored.json()["stale"] is True and restored.json()["position"]["page"] == 3
    assert (
        client.put(
            f"/api/v1/assets/{paper}/reading-position",
            json={"representation_id": rid, "page": 4, "source_hash": "a" * 64},
            headers={"If-Match": saved.headers["etag"]},
        ).status_code
        == 409
    )
