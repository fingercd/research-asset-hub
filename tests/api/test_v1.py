from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import (
    Activity,
    AssetRedirect,
    Base,
    DocumentChunk,
    FieldClaim,
    IndexState,
    Job,
    Representation,
)


@pytest.fixture
def api_runtime(tmp_path: Path) -> Iterator[tuple[TestClient, DatabaseManager, Engine]]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="g" * 64,
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
    )
    settings.sources_config_path.write_text(
        "schema_version: 1\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )
    settings.taxonomy_config_path.write_text(
        ("schema_version: 1\ndomains:\n  - id: agent\n    label: Agent 学习\n"),
        encoding="utf-8",
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    app = create_app(settings, database=database)
    with TestClient(app) as client:
        yield client, database, engine
    engine.dispose()


def create_asset(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Graph Agents",
        "asset_type": "paper",
        "authors": ["Ada"],
        "year": 2025,
        "domain_slugs": ["agent"],
        "summary": "A paper about graph-based agents.",
    }
    payload.update(overrides)
    response = client.post("/api/v1/assets", json=payload)
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def test_asset_lifecycle_etag_idempotency_and_audit(api_runtime: tuple[Any, ...]) -> None:
    client, database, _ = api_runtime
    payload = {
        "title": "Durable Research Asset",
        "asset_type": "paper",
        "domain_slugs": ["agent"],
    }
    first = client.post(
        "/api/v1/assets",
        json=payload,
        headers={"Idempotency-Key": "asset-once"},
    )
    replay = client.post(
        "/api/v1/assets",
        json=payload,
        headers={"Idempotency-Key": "asset-once"},
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert first.headers["ETag"] == 'W/"1"'

    reused = client.post(
        "/api/v1/assets",
        json={**payload, "title": "Different"},
        headers={"Idempotency-Key": "asset-once"},
    )
    assert reused.status_code == 409
    assert reused.headers["content-type"].startswith("application/problem+json")

    asset_id = first.json()["id"]
    stale = client.patch(
        f"/api/v1/assets/{asset_id}",
        json={"title": "Changed"},
        headers={"If-Match": 'W/"999"'},
    )
    assert stale.status_code == 412

    updated = client.patch(
        f"/api/v1/assets/{asset_id}",
        json={"title": "Changed"},
        headers={"If-Match": first.headers["ETag"]},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Changed"
    assert updated.headers["ETag"] != first.headers["ETag"]

    deleted = client.delete(f"/api/v1/assets/{asset_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/assets/{asset_id}").status_code == 404
    restored = client.post(f"/api/v1/assets/{asset_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None

    with database.session() as session:
        audit_count = session.execute(Base.metadata.tables["audit_events"].select()).all()
        outbox_count = session.execute(Base.metadata.tables["outbox_events"].select()).all()
    assert len(audit_count) >= 4
    assert len(outbox_count) >= 4


def test_reading_annotations_revision_search_and_dashboard(
    api_runtime: tuple[Any, ...],
) -> None:
    client, _, _ = api_runtime
    asset = create_asset(client)
    asset_id = asset["id"]

    reading = client.put(
        f"/api/v1/assets/{asset_id}/reading-state",
        json={"status": "reading"},
    )
    assert reading.status_code == 200
    assert reading.json()["reading_status"] == "reading"

    annotation = client.post(
        "/api/v1/annotations",
        json={
            "asset_id": asset_id,
            "kind": "comment",
            "summary": "关键批语",
            "body_markdown": "这篇论文的核心是关系图上的规划。",
        },
    )
    assert annotation.status_code == 201
    annotation_id = annotation.json()["id"]
    missing_precondition = client.patch(
        f"/api/v1/annotations/{annotation_id}",
        json={"body_markdown": "不应写入。"},
    )
    assert missing_precondition.status_code == 428
    changed = client.patch(
        f"/api/v1/annotations/{annotation_id}",
        json={"body_markdown": "更新后的详细批注。"},
        headers={"If-Match": annotation.headers["ETag"]},
    )
    assert changed.status_code == 200

    revisions = client.get(f"/api/v1/annotations/{annotation_id}/revisions")
    assert revisions.status_code == 200
    assert revisions.json()["items"][0]["body_markdown"].startswith("这篇论文")

    search = client.get("/api/v1/search", params={"q": "Graph Agents"})
    assert search.status_code == 200
    assert search.json()["items"][0]["annotation_summary"] == "关键批语"

    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["stats"]["reading"] == 1
    assert dashboard.json()["stats"]["annotations"] == 1

    domain = client.get("/api/v1/domains/agent")
    assert domain.status_code == 200
    assert domain.json()["asset_count"] == 1


def test_problem_details_and_invalid_cursor(api_runtime: tuple[Any, ...]) -> None:
    client, _, _ = api_runtime
    invalid_body = client.post("/api/v1/assets", json={"title": ""})
    assert invalid_body.status_code == 422
    assert invalid_body.headers["content-type"].startswith("application/problem+json")
    assert invalid_body.json()["errors"]

    invalid_cursor = client.get("/api/v1/assets", params={"cursor": "not-a-cursor"})
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["type"].endswith(":cursor")


def test_relation_crud_evidence_and_logical_deduplication(
    api_runtime: tuple[Any, ...],
) -> None:
    client, database, _ = api_runtime
    subject = create_asset(client, title="Subject")
    target = create_asset(client, title="Target")
    payload = {
        "subject_asset_id": subject["id"],
        "object_asset_id": target["id"],
        "relation_type": "implemented_by",
        "confirmation_status": "candidate",
        "confidence": 0.8,
    }
    created = client.post(
        "/api/v1/relations",
        json=payload,
        headers={"Idempotency-Key": "relation-once"},
    )
    replay = client.post(
        "/api/v1/relations",
        json=payload,
        headers={"Idempotency-Key": "relation-once"},
    )
    assert created.status_code == 201
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == created.json()["id"]
    relation_id = created.json()["id"]
    default_graph = client.get(
        "/api/v1/graph/neighbors",
        params={"asset_id": subject["id"]},
    )
    candidate_graph = client.get(
        "/api/v1/graph/neighbors",
        params={"asset_id": subject["id"], "relation_status": "candidate"},
    )
    assert default_graph.json()["edges"] == []
    assert len(candidate_graph.json()["edges"]) == 1

    duplicate = client.post("/api/v1/relations", json=payload)
    assert duplicate.status_code == 409
    missing_precondition = client.patch(
        f"/api/v1/relations/{relation_id}",
        json={"confirmation_status": "confirmed"},
    )
    assert missing_precondition.status_code == 428
    confirmed = client.patch(
        f"/api/v1/relations/{relation_id}",
        json={"confirmation_status": "confirmed"},
        headers={"If-Match": created.headers["ETag"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmation_status"] == "confirmed"
    confirmed_graph = client.get(
        "/api/v1/graph/neighbors",
        params={"asset_id": subject["id"]},
    )
    assert len(confirmed_graph.json()["edges"]) == 1

    evidence_payload = {
        "provider": "manual",
        "source_key": "local-note",
        "evidence": {"note": "Repository link checked by the local user."},
        "confidence": 1.0,
    }
    evidence = client.post(
        f"/api/v1/relations/{relation_id}/evidence",
        json=evidence_payload,
        headers={"Idempotency-Key": "evidence-once"},
    )
    evidence_replay = client.post(
        f"/api/v1/relations/{relation_id}/evidence",
        json=evidence_payload,
        headers={"Idempotency-Key": "evidence-once"},
    )
    assert evidence.status_code == 201
    assert evidence_replay.headers["Idempotency-Replayed"] == "true"
    assert evidence.json()["id"] == evidence_replay.json()["id"]
    evidence_list = client.get(f"/api/v1/relations/{relation_id}/evidence")
    assert evidence_list.status_code == 200
    assert evidence_list.json()["total"] == 1

    deleted = client.delete(
        f"/api/v1/relations/{relation_id}",
        headers={"If-Match": confirmed.headers["ETag"]},
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/relations/{relation_id}").status_code == 404
    tombstone = client.get(
        f"/api/v1/relations/{relation_id}",
        params={"include_deleted": True},
    )
    assert tombstone.status_code == 200
    restored = client.post(
        f"/api/v1/relations/{relation_id}/restore",
        headers={"If-Match": tombstone.headers["ETag"]},
    )
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None

    with database.session() as session:
        audit_rows = session.execute(
            Base.metadata.tables["audit_events"]
            .select()
            .where(Base.metadata.tables["audit_events"].c.entity_type == "relation")
        ).all()
        outbox_rows = session.execute(
            Base.metadata.tables["outbox_events"]
            .select()
            .where(Base.metadata.tables["outbox_events"].c.aggregate_type == "relation")
        ).all()
    assert len(audit_rows) >= 4
    assert len(outbox_rows) >= 4


def test_field_override_lifecycle_is_recoverable(api_runtime: tuple[Any, ...]) -> None:
    client, database, _ = api_runtime
    asset = create_asset(client, title="Imported title")
    payload = {
        "entity_id": asset["id"],
        "field_path": "/title",
        "override_value": "My preferred title",
        "reason": "Personal catalog wording",
    }
    created = client.post(
        "/api/v1/overrides",
        json=payload,
        headers={"Idempotency-Key": "override-once"},
    )
    replay = client.post(
        "/api/v1/overrides",
        json=payload,
        headers={"Idempotency-Key": "override-once"},
    )
    assert created.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    override_id = created.json()["id"]
    assert created.json()["active"] is True
    assert client.get(f"/api/v1/assets/{asset['id']}").json()["title"] == ("My preferred title")
    assert client.post("/api/v1/overrides", json=payload).status_code == 409
    forbidden = client.post(
        "/api/v1/overrides",
        json={
            "entity_id": asset["id"],
            "field_path": "/row_version",
            "override_value": 99,
        },
    )
    assert forbidden.status_code == 422

    missing_precondition = client.patch(
        f"/api/v1/overrides/{override_id}",
        json={"override_value": "Unsafe change"},
    )
    assert missing_precondition.status_code == 428
    changed = client.patch(
        f"/api/v1/overrides/{override_id}",
        json={"override_value": "Reviewed preferred title"},
        headers={"If-Match": created.headers["ETag"]},
    )
    assert changed.status_code == 200
    assert changed.json()["override_value"] == "Reviewed preferred title"
    assert client.get(f"/api/v1/assets/{asset['id']}").json()["title"] == (
        "Reviewed preferred title"
    )

    with database.session() as session:
        session.add(
            FieldClaim(
                entity_type="asset",
                entity_id=UUID(asset["id"]),
                field_path="/title",
                value_json="New upstream title",
                value_digest="c" * 64,
                provider="test-importer",
                source_pointer="/asset/title",
                source_key="test-import:v2",
                authority=100,
                confidence=1.0,
                status="active",
            )
        )

    deleted = client.delete(
        f"/api/v1/overrides/{override_id}",
        headers={"If-Match": changed.headers["ETag"]},
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/overrides/{override_id}").status_code == 404
    cleared = client.get(
        f"/api/v1/overrides/{override_id}",
        params={"include_cleared": True},
    )
    assert cleared.status_code == 200
    assert cleared.json()["resolution_kind"] == "cleared"
    assert cleared.json()["override_value"] == "Reviewed preferred title"
    assert client.get(f"/api/v1/assets/{asset['id']}").json()["title"] == ("New upstream title")
    restored = client.post(
        f"/api/v1/overrides/{override_id}/restore",
        headers={"If-Match": cleared.headers["ETag"]},
    )
    assert restored.status_code == 200
    assert restored.json()["active"] is True
    assert client.get(f"/api/v1/assets/{asset['id']}").json()["title"] == (
        "Reviewed preferred title"
    )
    listed = client.get("/api/v1/overrides", params={"entity_id": asset["id"]})
    assert listed.json()["total"] == 1

    with database.session() as session:
        audit_rows = session.execute(
            Base.metadata.tables["audit_events"]
            .select()
            .where(Base.metadata.tables["audit_events"].c.entity_type == "field_override")
        ).all()
        outbox_rows = session.execute(
            Base.metadata.tables["outbox_events"]
            .select()
            .where(Base.metadata.tables["outbox_events"].c.aggregate_type == "field_override")
        ).all()
    assert len(audit_rows) == 4
    assert len(outbox_rows) == 4


def test_asset_redirects_resolve_to_canonical_resources(
    api_runtime: tuple[Any, ...],
) -> None:
    client, database, _ = api_runtime
    legacy = create_asset(client, title="Legacy duplicate")
    canonical = create_asset(client, title="Canonical paper")
    with database.session() as session:
        session.add(
            AssetRedirect(
                from_asset_id=UUID(legacy["id"]),
                to_asset_id=UUID(canonical["id"]),
                reason="approved strong-identifier merge",
            )
        )
        representation = Representation(
            asset_id=UUID(canonical["id"]),
            representation_type="pdf",
            role="original",
            materialization_kind="url",
            availability="available",
            root_key="source",
            external_url="https://example.invalid/canonical.pdf",
            attributes={},
        )
        session.add(representation)
        session.flush()
        representation_id = str(representation.id)

    redirected = client.get(f"/api/v1/assets/{legacy['id']}")
    assert redirected.status_code == 200
    assert redirected.json()["id"] == canonical["id"]
    assert redirected.headers["X-Asset-Redirect"] == "permanent"
    assert redirected.headers["X-Canonical-Asset-ID"] == canonical["id"]
    updated = client.patch(
        f"/api/v1/assets/{legacy['id']}",
        json={"title": "Updated through legacy ID"},
        headers={"If-Match": redirected.headers["ETag"]},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == canonical["id"]
    assert client.get(f"/api/v1/assets/{canonical['id']}").json()["title"] == (
        "Updated through legacy ID"
    )
    representations = client.get(f"/api/v1/assets/{legacy['id']}/representations")
    assert representations.status_code == 200
    assert representations.json()["items"][0]["id"] == representation_id
    redirect = client.get(f"/api/v1/asset-redirects/{legacy['id']}")
    assert redirect.status_code == 200
    assert redirect.json()["canonical_asset_id"] == canonical["id"]
    assert redirect.json()["permanent"] is True


def test_extraction_and_index_status_interfaces(api_runtime: tuple[Any, ...]) -> None:
    client, database, _ = api_runtime
    asset = create_asset(client, title="Status asset")
    with database.session() as session:
        representation = Representation(
            asset_id=UUID(asset["id"]),
            representation_type="pdf",
            role="original",
            materialization_kind="url",
            availability="available",
            root_key="source",
            external_url="https://example.invalid/paper.pdf",
            attributes={},
        )
        session.add(representation)
        session.flush()
        job = Job(
            job_type="extract_representation",
            parameters={"representation_id": str(representation.id)},
            status="succeeded",
            progress=1.0,
            attempts=1,
            max_attempts=3,
            dedupe_key=f"test-extraction:{representation.id}",
        )
        session.add(job)
        session.flush()
        activity = Activity(
            job_id=job.id,
            asset_id=representation.asset_id,
            representation_id=representation.id,
            activity_type="extraction",
            tool_name="test-extractor",
            tool_version="1",
            status="succeeded",
            result={
                "status": "partial",
                "file_count": 1,
                "chunk_count": 1,
                "ocr_states": ["not_needed"],
                "warnings": ["sample warning"],
            },
        )
        session.add(activity)
        session.add(
            DocumentChunk(
                asset_id=representation.asset_id,
                representation_id=representation.id,
                activity_id=activity.id,
                chunk_index=0,
                chunk_kind="pdf",
                text_content="Extracted text",
                content_hash="a" * 64,
                parser_name="test",
                parser_version="1",
            )
        )
        index_state = IndexState(
            index_name="documents",
            index_kind="vector",
            entity_type="representation",
            entity_id=representation.id,
            content_digest="b" * 64,
            status="ready",
            provider="local",
            model="BAAI/bge-m3",
            dimension=1024,
            version="bge_m3_v1",
        )
        session.add(index_state)
        session.flush()
        job_id = str(job.id)
        representation_id = str(representation.id)
        index_state_id = str(index_state.id)

    extractions = client.get(
        "/api/v1/extractions",
        params={"representation_id": representation_id},
    )
    assert extractions.status_code == 200
    assert extractions.json()["total"] == 1
    extraction = extractions.json()["items"][0]
    assert extraction["status"] == "succeeded"
    assert extraction["extraction_status"] == "partial"
    assert extraction["chunk_count"] == 1
    assert extraction["warnings"] == ["sample warning"]
    extraction_detail = client.get(f"/api/v1/extractions/{job_id}")
    assert extraction_detail.status_code == 200
    assert extraction_detail.headers["ETag"] == 'W/"1"'

    indexes = client.get(
        "/api/v1/indexes",
        params={"version": "bge_m3_v1", "status": "ready"},
    )
    assert indexes.status_code == 200
    assert indexes.json()["total"] == 1
    assert indexes.json()["counts"]["by_status"] == {"ready": 1}
    assert indexes.json()["items"][0]["dimension"] == 1024
    index_detail = client.get(f"/api/v1/indexes/{index_state_id}")
    assert index_detail.status_code == 200
    assert index_detail.json()["entity_id"] == representation_id


def test_chat_stream_emits_grounded_event_sequence(
    api_runtime: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_runtime
    asset = create_asset(client, title="Streaming evidence")

    def fake_answer_with_rag(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            answer=SimpleNamespace(
                text="基于本地证据的回答。",
                citations=(),
                provider="test-provider",
                model="test-model",
            ),
            retrieved=(
                SimpleNamespace(
                    asset_id=asset["id"],
                    title=asset["title"],
                    representation_id=str(UUID(int=10)),
                    chunk_id=str(UUID(int=11)),
                    anchor={"kind": "pdf", "page": 1},
                    score=0.95,
                    sources=("keyword", "vector"),
                    url="/api/v1/representations/test/open-actions",
                ),
            ),
            stop_reason="completed",
            audit={"prompt_version": "test"},
        )

    monkeypatch.setattr(
        "apps.api.routes_v1.answer_with_rag",
        fake_answer_with_rag,
    )
    response = client.post(
        "/api/v1/chat/stream",
        json={"message": "这篇论文讲了什么？", "asset_ids": [asset["id"]]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.index("event: evidence") < response.text.index("event: answer")
    assert response.text.index("event: answer") < response.text.index("event: done")
    assert "基于本地证据的回答。" in response.text
    assert '"stop_reason":"completed"' in response.text


def test_unmigrated_database_is_explicitly_degraded() -> None:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_database_engine(settings)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    app = create_app(settings, database=database)
    with TestClient(app) as client:
        response = client.get("/api/v1/assets")
    engine.dispose()
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith(":database-degraded")
    assert "missing required tables" in response.json()["detail"]
