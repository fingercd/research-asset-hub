from __future__ import annotations

import json
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
    Asset,
    AssetRedirect,
    Base,
    DocumentChunk,
    FieldClaim,
    IndexState,
    Job,
    Relation,
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


def install_fake_search_runtime(
    monkeypatch: pytest.MonkeyPatch,
    hits_by_query: dict[str, list[tuple[str, float]]],
    *,
    observed_options: list[dict[str, Any]] | None = None,
) -> None:
    class FakeSearchService:
        def search(self, query: str, **options: Any) -> SimpleNamespace:
            if observed_options is not None:
                observed_options.append(dict(options))
            hits = [
                SimpleNamespace(
                    document=SimpleNamespace(
                        asset_id=asset_id,
                        text=f"Search evidence for {query} in {asset_id}.",
                        kind="asset",
                        metadata={},
                    ),
                    score=score,
                    source="keyword",
                    exact_match=False,
                )
                for asset_id, score in hits_by_query.get(query, [])
            ]
            hits = hits[: int(options.get("limit", len(hits)))]
            return SimpleNamespace(
                hits=hits,
                degraded=False,
                errors=(),
                embedding_status=None,
                reranker_status=None,
            )

    runtime = SimpleNamespace(service=FakeSearchService())
    monkeypatch.setattr("apps.api.routes_v1._search_runtime", lambda _request: runtime)


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_runtime
    asset = create_asset(client)
    asset_id = asset["id"]
    install_fake_search_runtime(
        monkeypatch,
        {"Graph Agents": [(asset_id, 1.0)]},
    )

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
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["stats"]["reading"] == 1
    assert dashboard.json()["stats"]["annotations"] == 1

    domain = client.get("/api/v1/domains/agent")
    assert domain.status_code == 200
    assert domain.json()["asset_count"] == 1
    portal = client.get("/api/v1/domains/agent/portal")
    assert portal.status_code == 200, portal.text
    assert portal.headers["Cache-Control"] == "no-store"
    assert portal.json()["renderer"] == "v2"
    assert portal.json()["domain"]["asset_count"] == 1
    assert [item["id"] for item in portal.json()["portal"]["sections"]] == [
        "scope",
        "timeline",
        "capability-method-map",
        "landmarks",
        "benchmarks",
        "reading-routes",
        "catalog",
        "frontiers",
        "evidence",
    ]


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


def test_paper_grouping_confirmed_relations_actions_and_raw_compatibility(
    api_runtime: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, _ = api_runtime
    papers = [
        create_asset(client, title=f"Fanout Paper {index}", year=2020 + index) for index in range(5)
    ]
    candidate_only_paper = create_asset(client, title="Candidate-only Paper")
    shared_repository = create_asset(
        client,
        title="Shared Implementation",
        asset_type="repository",
        authors=[],
        year=None,
        domain_slugs=["code-domain"],
        summary="Code shared by five papers.",
    )
    candidate_repository = create_asset(
        client,
        title="Candidate Implementation",
        asset_type="repository",
        authors=[],
        year=None,
        summary="Unconfirmed code candidate.",
    )
    for paper in papers:
        response = client.post(
            "/api/v1/relations",
            json={
                "subject_asset_id": paper["id"],
                "object_asset_id": shared_repository["id"],
                "relation_type": "implemented_by",
                "confirmation_status": "confirmed",
            },
        )
        assert response.status_code == 201
    candidate_relation = client.post(
        "/api/v1/relations",
        json={
            "subject_asset_id": candidate_only_paper["id"],
            "object_asset_id": candidate_repository["id"],
            "relation_type": "implemented_by",
            "confirmation_status": "candidate",
        },
    )
    assert candidate_relation.status_code == 201

    with database.session() as session:
        local_pdf = Representation(
            asset_id=UUID(papers[0]["id"]),
            representation_type="pdf",
            role="original",
            materialization_kind="file",
            availability="available",
            root_key="source",
            path_ref="papers/local.pdf",
            network_required=False,
            attributes={},
        )
        remote_pdf = Representation(
            asset_id=UUID(papers[0]["id"]),
            representation_type="pdf",
            role="primary",
            materialization_kind="url",
            availability="available",
            root_key="source",
            external_url="https://example.invalid/paper.pdf",
            network_required=True,
            attributes={},
        )
        missing_html = Representation(
            asset_id=UUID(papers[0]["id"]),
            representation_type="html",
            role="official",
            materialization_kind="file",
            availability="missing",
            root_key="source",
            path_ref="papers/missing.html",
            network_required=False,
            attributes={"missing_reason": "HTML export was not generated."},
        )
        guide_html = Representation(
            asset_id=UUID(papers[1]["id"]),
            representation_type="html",
            role="guide",
            materialization_kind="file",
            availability="available",
            root_key="source",
            path_ref="papers/guide.html",
            network_required=False,
            attributes={},
        )
        primary_html = Representation(
            asset_id=UUID(papers[2]["id"]),
            representation_type="html",
            role="official",
            materialization_kind="file",
            availability="available",
            root_key="source",
            path_ref="papers/document.html",
            network_required=False,
            attributes={},
        )
        shared_code = Representation(
            asset_id=UUID(shared_repository["id"]),
            representation_type="repository",
            role="official",
            materialization_kind="url",
            availability="available",
            root_key="source",
            external_url="https://example.invalid/shared.git",
            network_required=True,
            attributes={},
        )
        candidate_code = Representation(
            asset_id=UUID(candidate_repository["id"]),
            representation_type="repository",
            role="official",
            materialization_kind="directory",
            availability="available",
            root_key="source",
            path_ref="repos/candidate",
            network_required=False,
            attributes={},
        )
        session.add_all(
            [
                local_pdf,
                remote_pdf,
                missing_html,
                guide_html,
                primary_html,
                shared_code,
                candidate_code,
            ]
        )
        session.flush()
        representation_ids = {
            "local_pdf": str(local_pdf.id),
            "remote_pdf": str(remote_pdf.id),
            "missing_html": str(missing_html.id),
            "guide_html": str(guide_html.id),
            "primary_html": str(primary_html.id),
            "shared_code": str(shared_code.id),
        }

    query = "shared implementation"
    install_fake_search_runtime(
        monkeypatch,
        {
            query: [
                (shared_repository["id"], 0.93),
                (papers[0]["id"], 0.72),
            ],
            "candidate implementation": [(candidate_repository["id"], 0.88)],
        },
    )

    grouped = client.get(
        "/api/v1/search",
        params={"q": query, "group_by": "paper", "type": "all"},
    )
    assert grouped.status_code == 200
    grouped_payload = grouped.json()
    assert grouped_payload["group_by"] == "paper"
    assert grouped_payload["total"] == 5
    assert grouped_payload["facets"]["types"] == {"paper": 5}
    assert grouped_payload["facets"]["domains"] == {"agent": 5}
    assert {item["unit_id"] for item in grouped_payload["items"]} == {
        paper["id"] for paper in papers
    }
    assert all(item["unit_kind"] == "paper" for item in grouped_payload["items"])
    assert all(
        [repository["id"] for repository in item["related_repositories"]]
        == [shared_repository["id"]]
        for item in grouped_payload["items"]
    )
    assert all(item["covered_asset_count"] == 2 for item in grouped_payload["items"])
    assert not any(item["unit_id"] == shared_repository["id"] for item in grouped_payload["items"])

    first_unit = next(
        item for item in grouped_payload["items"] if item["unit_id"] == papers[0]["id"]
    )
    assert first_unit["score"] == 0.93
    assert first_unit["match_source"] == "paper_and_related_repository"
    assert [item["id"] for item in first_unit["matched_assets"]] == [
        papers[0]["id"],
        shared_repository["id"],
    ]
    repository_only_unit = next(
        item for item in grouped_payload["items"] if item["unit_id"] == papers[1]["id"]
    )
    assert repository_only_unit["match_source"] == "related_repository"
    assert [item["id"] for item in repository_only_unit["matched_assets"]] == [
        shared_repository["id"]
    ]

    actions = {action["kind"]: action for action in first_unit["quick_actions"]}
    assert list(actions) == ["pdf", "html", "guide", "code"]
    assert actions["pdf"]["status"] == "available"
    assert actions["pdf"]["representation_id"] == representation_ids["local_pdf"]
    assert actions["pdf"]["alternatives"] == [
        {
            "representation_id": representation_ids["remote_pdf"],
            "owner_asset_id": papers[0]["id"],
            "label": "primary",
            "role": "primary",
            "status": "network_required",
            "relation_type": None,
        }
    ]
    assert actions["html"]["status"] == "missing"
    assert actions["html"]["representation_id"] == representation_ids["missing_html"]
    assert actions["html"]["reason"] == "HTML export was not generated."
    assert actions["html"]["alternatives"] == []
    unavailable_open = client.post(
        f"/api/v1/representations/{representation_ids['missing_html']}/open-actions"
    )
    assert unavailable_open.status_code == 409
    assert unavailable_open.headers["content-type"].startswith("application/problem+json")
    assert actions["guide"]["status"] == "not_provided"
    assert actions["guide"]["representation_id"] is None
    assert actions["code"]["status"] == "network_required"
    assert actions["code"]["representation_id"] == representation_ids["shared_code"]
    assert actions["code"]["owner_asset_id"] == shared_repository["id"]
    assert actions["code"]["relation_type"] == "implemented_by"

    candidate = client.get(
        "/api/v1/search",
        params={
            "q": "candidate implementation",
            "group_by": "paper",
            "type": "all",
        },
    )
    assert candidate.status_code == 200
    assert candidate.json()["total"] == 1
    candidate_unit = candidate.json()["items"][0]
    assert candidate_unit["unit_id"] == candidate_repository["id"]
    assert candidate_unit["unit_kind"] == "repository"
    assert candidate_unit["match_source"] == "repository"
    assert candidate_unit["related_repositories"] == []

    no_confirmed_code = client.get(
        "/api/v1/search",
        params={"group_by": "paper", "type": "paper", "has_code": False},
    )
    assert no_confirmed_code.status_code == 200
    assert no_confirmed_code.json()["total"] == 1
    candidate_parent_unit = no_confirmed_code.json()["items"][0]
    assert candidate_parent_unit["unit_id"] == candidate_only_paper["id"]
    assert candidate_parent_unit["related_repositories"] == []
    candidate_parent_actions = {
        action["kind"]: action for action in candidate_parent_unit["quick_actions"]
    }
    assert candidate_parent_actions["pdf"]["status"] == "not_provided"
    assert candidate_parent_actions["code"]["status"] == "not_provided"
    assert candidate_parent_actions["code"]["relation_type"] == "implemented_by"
    assert candidate_parent_actions["code"]["reason"] == "没有已确认且可用的代码仓库。"

    grouped_html = client.get(
        "/api/v1/search",
        params={
            "group_by": "paper",
            "type": "paper",
            "has_html": True,
        },
    )
    assert grouped_html.status_code == 200
    assert grouped_html.json()["total"] == 1
    html_unit = grouped_html.json()["items"][0]
    assert html_unit["unit_id"] == papers[2]["id"]
    html_actions = {action["kind"]: action for action in html_unit["quick_actions"]}
    assert html_actions["html"]["representation_id"] == representation_ids["primary_html"]

    grouped_guide = client.get(
        "/api/v1/search",
        params={
            "group_by": "paper",
            "type": "paper",
            "has_guide": True,
        },
    )
    assert grouped_guide.status_code == 200
    assert grouped_guide.json()["total"] == 1
    guide_unit = grouped_guide.json()["items"][0]
    assert guide_unit["unit_id"] == papers[1]["id"]
    guide_actions = {action["kind"]: action for action in guide_unit["quick_actions"]}
    assert guide_actions["html"]["status"] == "not_provided"
    assert guide_actions["guide"]["representation_id"] == representation_ids["guide_html"]

    raw_html = client.get(
        "/api/v1/search",
        params={"group_by": "none", "type": "paper", "has_html": True},
    )
    assert raw_html.status_code == 200
    assert raw_html.json()["total"] == 1
    assert raw_html.json()["items"][0]["id"] == papers[2]["id"]

    related_repository_domain = client.get(
        "/api/v1/search",
        params={
            "q": query,
            "group_by": "paper",
            "type": "all",
            "domain": "code-domain",
        },
    )
    assert related_repository_domain.status_code == 200
    assert related_repository_domain.json()["total"] == 5
    assert all(item["unit_kind"] == "paper" for item in related_repository_domain.json()["items"])

    raw_default = client.get("/api/v1/search", params={"q": query})
    raw_explicit = client.get(
        "/api/v1/search",
        params={"q": query, "group_by": "none"},
    )
    assert raw_default.status_code == 200
    assert raw_default.json() == raw_explicit.json()
    assert "group_by" not in raw_default.json()
    assert "unit_id" not in raw_default.json()["items"][0]
    assert "representations" in raw_default.json()["items"][0]

    repository_raw = client.get(
        "/api/v1/search",
        params={"q": query, "group_by": "paper", "type": "repository"},
    )
    assert repository_raw.status_code == 200
    assert repository_raw.json()["total"] == 1
    assert repository_raw.json()["items"][0]["id"] == shared_repository["id"]
    assert "unit_id" not in repository_raw.json()["items"][0]

    assets_default = client.get("/api/v1/assets")
    assets_with_representations = client.get(
        "/api/v1/assets",
        params={"include": "representations"},
    )
    assert assets_default.status_code == 200
    assert assets_with_representations.status_code == 200
    assert all("representations" not in item for item in assets_default.json()["items"])
    included_by_id = {item["id"]: item for item in assets_with_representations.json()["items"]}
    assert {
        representation["id"]
        for representation in included_by_id[papers[0]["id"]]["representations"]
    } == {
        representation_ids["local_pdf"],
        representation_ids["remote_pdf"],
        representation_ids["missing_html"],
    }


def test_paper_grouping_paginates_after_fanout_without_duplicates(
    api_runtime: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, _ = api_runtime
    with database.session() as session:
        repository = Asset(
            asset_type="repository",
            title="Pagination Repository",
            authors=[],
            domain_slugs=["agent"],
            tags=[],
            source_urls=[],
            attributes={},
            analysis={},
        )
        papers = [
            Asset(
                asset_type="paper",
                title=f"Pagination Paper {index:02d}",
                authors=[],
                year=2025,
                domain_slugs=["agent"],
                tags=["pagination"],
                source_urls=[],
                attributes={},
                analysis={},
            )
            for index in range(31)
        ]
        session.add_all([repository, *papers])
        session.flush()
        session.add_all(
            [
                Relation(
                    subject_asset_id=paper.id,
                    object_asset_id=repository.id,
                    relation_type="implemented_by",
                    confirmation_status="confirmed",
                    source_key="pagination-test",
                    evidence={},
                    attributes={},
                )
                for paper in papers
            ]
        )
        session.flush()
        repository_id = str(repository.id)

    install_fake_search_runtime(monkeypatch, {"pagination fanout": [(repository_id, 0.9)]})
    first = client.get(
        "/api/v1/search",
        params={
            "q": "pagination fanout",
            "group_by": "paper",
            "type": "paper",
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["total"] == 31
    assert len(first_payload["items"]) == 30
    assert first_payload["next_cursor"] is not None
    assert first_payload["facets"]["types"] == {"paper": 31}
    assert first_payload["facets"]["tags"] == {"pagination": 31}

    second = client.get(
        "/api/v1/search",
        params={
            "q": "pagination fanout",
            "group_by": "paper",
            "type": "paper",
            "cursor": first_payload["next_cursor"],
        },
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["total"] == 31
    assert len(second_payload["items"]) == 1
    assert second_payload["next_cursor"] is None
    first_ids = {item["unit_id"] for item in first_payload["items"]}
    second_ids = {item["unit_id"] for item in second_payload["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 31

    repeated = client.get(
        "/api/v1/search",
        params={
            "q": "pagination fanout",
            "group_by": "paper",
            "type": "paper",
        },
    )
    assert [item["unit_id"] for item in repeated.json()["items"]] == [
        item["unit_id"] for item in first_payload["items"]
    ]


def test_paper_grouping_expands_candidate_window_beyond_one_hundred(
    api_runtime: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, _ = api_runtime
    with database.session() as session:
        papers = [
            Asset(
                asset_type="paper",
                title=f"Wide Candidate Paper {index:03d}",
                authors=[],
                year=2025,
                domain_slugs=["agent"],
                tags=["wide-candidate"],
                source_urls=[],
                attributes={},
                analysis={},
            )
            for index in range(121)
        ]
        session.add_all(papers)
        session.flush()
        paper_ids = [str(paper.id) for paper in papers]

    observed_options: list[dict[str, Any]] = []
    install_fake_search_runtime(
        monkeypatch,
        {
            "wide candidate": [
                (paper_id, 1.0 - index / 1000)
                for index, paper_id in enumerate(paper_ids)
            ]
        },
        observed_options=observed_options,
    )

    cursor: str | None = None
    seen: list[str] = []
    first_payload: dict[str, Any] | None = None
    while True:
        parameters = {
            "q": "wide candidate",
            "group_by": "paper",
            "type": "paper",
        }
        if cursor is not None:
            parameters["cursor"] = cursor
        response = client.get("/api/v1/search", params=parameters)
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        first_payload = first_payload or payload
        seen.extend(item["unit_id"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert first_payload is not None
    assert first_payload["total"] == 121
    assert first_payload["facets"]["types"] == {"paper": 121}
    assert first_payload["facets"]["domains"] == {"agent": 121}
    assert first_payload["facets"]["tags"] == {"wide-candidate": 121}
    assert len(seen) == 121
    assert len(set(seen)) == 121
    assert set(seen) == set(paper_ids)
    assert observed_options
    assert all(int(options["limit"]) >= 121 for options in observed_options)
    assert all(int(options["candidate_limit"]) >= 121 for options in observed_options)


def test_domain_overview_contract_hides_portal_and_supports_presentations(
    api_runtime: tuple[Any, ...],
) -> None:
    client, database, _ = api_runtime
    paper = create_asset(client, title="Portal Domain Paper")
    repository = create_asset(
        client,
        title="Portal Domain Repository",
        asset_type="repository",
        authors=[],
        year=None,
    )
    relation = client.post(
        "/api/v1/relations",
        json={
            "subject_asset_id": paper["id"],
            "object_asset_id": repository["id"],
            "relation_type": "implemented_by",
            "confirmation_status": "confirmed",
        },
    )
    assert relation.status_code == 201
    with database.session() as session:
        portal = Asset(
            asset_type="collection",
            title="Agent Domain Overview",
            authors=[],
            domain_slugs=["agent"],
            tags=[],
            source_urls=[],
            attributes={
                "kind": "domain-overview",
                "system_hidden": True,
            },
            analysis={},
        )
        session.add(portal)
        session.flush()
        overview = Representation(
            asset_id=portal.id,
            representation_type="html",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="private/portal/index.html",
            mime_type="text/html",
            attributes={"strict_mounts": True},
        )
        session.add(overview)
        session.flush()
        portal_id = str(portal.id)
        representation_id = str(overview.id)

    domains = client.get("/api/v1/domains")
    assert domains.status_code == 200
    summary = domains.json()["items"][0]
    assert summary["slug"] == "agent"
    assert summary["asset_count"] == 2
    assert summary["paper_count"] == 1
    assert summary["repository_count"] == 1
    assert summary["research_unit_count"] == 1
    assert summary["reading_counts"] == {"unread": 2, "reading": 0, "read": 0}
    assert summary["overview_status"] == "available"
    assert "overview" not in summary

    detail_response = client.get("/api/v1/domains/agent")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["overview"] == {
        "asset_id": portal_id,
        "representation_id": representation_id,
        "status": "available",
        "default_presentation": "hub",
        "last_verified_at": None,
        "warning": None,
    }
    assert portal_id not in {asset["id"] for asset in detail["recent_assets"]}
    serialized_detail = json.dumps(detail)
    assert "private/portal/index.html" not in serialized_detail
    assert "path_ref" not in serialized_detail

    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["stats"]["assets"] == 2
    assert portal_id not in {
        asset["id"] for asset in dashboard.json()["recent_assets"]
    }
    listed_assets = client.get("/api/v1/assets")
    assert listed_assets.status_code == 200
    assert listed_assets.json()["total"] == 2
    assert client.get(f"/api/v1/assets/{portal_id}").status_code == 404
    hidden_comparison = client.post(
        "/api/v1/comparisons",
        json={"asset_ids": [paper["id"], portal_id]},
    )
    assert hidden_comparison.status_code == 404
    raw_search = client.get("/api/v1/search")
    assert raw_search.status_code == 200
    assert raw_search.json()["total"] == 2

    compatible = client.post(
        f"/api/v1/representations/{representation_id}/open-actions"
    )
    assert compatible.status_code == 200
    assert compatible.json()["presentation"] == "original"
    assert "presentation=" not in compatible.json()["url"]
    themed = client.post(
        f"/api/v1/representations/{representation_id}/open-actions",
        json={"presentation": "hub", "embed": True},
    )
    assert themed.status_code == 200
    assert themed.json()["presentation"] == "hub"
    assert themed.json()["embed"] is True
    assert "&presentation=hub" in themed.json()["url"]
    invalid = client.post(
        f"/api/v1/representations/{representation_id}/open-actions",
        json={"presentation": "unsafe"},
    )
    assert invalid.status_code == 422
