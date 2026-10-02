from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.config import PROJECT_ROOT, ApiSettings
from apps.api.database import DatabaseManager
from apps.api.problem import install_problem_handlers
from apps.api.routes import knowledge
from apps.asset_gateway.registry import SourceConfigurationError, SourceRegistry
from packages.domain.models import (
    Activity,
    Asset,
    BackupRun,
    Base,
    DocumentChunk,
    ExternalIdentifierModel,
    Representation,
    Source,
    SourceRetirement,
)
from packages.domain.serving_scope import RetiredRepresentationError
from packages.indexing.keyword import KeywordSearchService
from packages.indexing.meili import InMemoryKeywordIndex
from packages.indexing.models import SearchDocument
from packages.knowledge.documents import document_from_chunks, text_hash
from packages.knowledge.query_terms import QueryPlan
from packages.knowledge.service import KnowledgeError, KnowledgeService, representation_source_type


@pytest.fixture
def library(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            alias="test", label="Test", adapter="generic_manifest", root_path=str(tmp_path)
        )
        asset = Asset(asset_type="paper", title="Evidence Paper", domain_slugs=["agent"], year=2026)
        session.add_all([source, asset])
        session.flush()
        original_path = tmp_path / "paper.pdf"
        original_path.write_bytes(b"PDF fixture")
        rep = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="pdf",
            role="original",
            path_ref="paper.pdf",
            availability="available",
            sha256=hashlib.sha256(original_path.read_bytes()).hexdigest(),
        )
        session.add(rep)
        session.flush()
        for index, text in enumerate(["first page text", "second page text"]):
            session.add(
                DocumentChunk(
                    asset_id=asset.id,
                    representation_id=rep.id,
                    chunk_index=index,
                    chunk_kind="pdf",
                    text_content=text,
                    content_hash=text_hash(text),
                    parser_name="pdf",
                    parser_version="1",
                    anchor_kind="pdf_page",
                    anchor={"kind": "pdf", "page": index + 1},
                )
            )
        session.commit()
        registry = SourceRegistry({("test", "source"): tmp_path})
        service = KnowledgeService(session, registry, tmp_path / "cache")
        yield service, asset, rep, source, tmp_path
    engine.dispose()


def test_full_text_continuation_and_page_anchors(library):
    service, asset, rep, _, _ = library
    expected = "first page text\n\nsecond page text"
    offset = 0
    fragments = []
    while True:
        result = service.read_evidence(
            asset.id, representation_id=rep.id, offset=offset, max_chars=5
        )
        fragments.append(result["text"])
        assert sum(len(segment["text"]) for segment in result["segments"]) <= 5
        for segment in result["segments"]:
            assert segment["anchor"]["precision"] == "page"
            assert segment["evidence_id"].startswith("evidence:")
        if not result["has_more"]:
            break
        offset = result["next_offset"]
    assert "".join(fragments) == expected
    page = service.read_evidence(asset.id, page_start=2, page_end=2)
    assert page["text"] == "second page text"
    assert json.loads(json.dumps(page))["asset_id"] == str(asset.id)
    assert not service.session.dirty and not service.session.new


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 16001},
        {"max_chars": 0},
        {"offset": -1},
        {"page_start": 2, "page_end": 1},
        {"source_type": "all"},
    ],
)
def test_read_limits_fail_closed(library, kwargs):
    service, asset, *_ = library
    with pytest.raises(KnowledgeError) as error:
        service.read_evidence(asset.id, **kwargs)
    assert error.value.status == 422


def test_owner_mismatch_and_deleted_asset(library):
    service, asset, rep, *_ = library
    other = Asset(asset_type="paper", title="Other")
    service.session.add(other)
    service.session.flush()
    with pytest.raises(KnowledgeError) as error:
        service.read_evidence(other.id, representation_id=rep.id)
    assert error.value.code == "representation_not_found"
    asset.deleted_at = datetime.now(UTC)
    with pytest.raises(KnowledgeError, match="asset is not available"):
        service.get_paper(asset.id)


@pytest.mark.parametrize("change", ["disabled", "missing", "changed", "traversal", "deleted"])
def test_source_loss_never_serves_existing_chunks(library, change):
    service, asset, rep, source, tmp_path = library
    if change == "disabled":
        source.enabled = False
    elif change == "missing":
        (tmp_path / "paper.pdf").unlink()
    elif change == "changed":
        (tmp_path / "paper.pdf").write_bytes(b"changed")
    elif change == "traversal":
        rep.path_ref = "../private.pdf"
    else:
        rep.deleted_at = datetime.now(UTC)
    with pytest.raises(KnowledgeError):
        service.read_evidence(asset.id, representation_id=rep.id)


def test_analysis_cannot_be_read_as_original(library):
    service, asset, rep, *_ = library
    rep.role = "analysis"
    with pytest.raises(KnowledgeError) as error:
        service.read_evidence(asset.id, representation_id=rep.id)
    assert error.value.code == "source_type_mismatch"
    assert service.read_evidence(asset.id, source_type="analysis")["source_type"] == "analysis"
    with pytest.raises(KnowledgeError):
        service.read_evidence(asset.id)


def test_cache_is_bound_to_live_representation_hash(library):
    service, asset, rep, _, tmp_path = library
    document = document_from_chunks(
        asset_id=str(asset.id),
        representation_id=str(rep.id),
        source_hash=rep.sha256,
        title=asset.title,
        source_type="original",
        chunks=[
            {
                "id": "cached",
                "chunk_index": 0,
                "text_content": "cached text",
                "anchor": {"kind": "pdf", "page": 1},
            }
        ],
    )
    folder = tmp_path / "cache" / str(asset.id)
    folder.mkdir(parents=True)
    (folder / "document.json").write_text(json.dumps(document), encoding="utf-8")
    assert service.read_evidence(asset.id)["text"] == "cached text"
    document["source_hash"] = "b" * 64
    (folder / "document.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(KnowledgeError) as error:
        service.read_evidence(asset.id, representation_id=rep.id)
    assert error.value.code == "stale_cache"


def test_retired_representation_is_rejected(library):
    service, asset, rep, source, _ = library
    backup = BackupRun(destination_label="test")
    service.session.add(backup)
    service.session.flush()
    service.session.add(
        SourceRetirement(
            source_id=source.id,
            backup_id=backup.id,
            payload={},
            payload_digest="a" * 64,
            frozen_manifest_sha256="a" * 64,
            frozen_source_config_sha256="a" * 64,
            signature_algorithm="test",
            signer_key_id="test",
            signature="test",
            reason="test",
            record_set_digest="a" * 64,
            representation_set_digest="a" * 64,
            mount_set_digest="a" * 64,
            collection_digest="a" * 64,
            applied_by="test",
        )
    )
    service.session.flush()
    with pytest.raises(RetiredRepresentationError):
        service.read_evidence(asset.id, representation_id=rep.id)
    assert service.get_paper(asset.id)["representations"] == []


def test_search_is_bounded_sql_and_uses_identifiers(library):
    service, asset, rep, *_ = library
    service.session.add_all(
        [
            ExternalIdentifierModel(
                asset_id=asset.id,
                scheme="doi",
                value="10.123/evidence",
                canonical="10.123/evidence",
            ),
            Asset(asset_type="paper", title="Evidence Hidden", attributes={"system_hidden": True}),
            Asset(asset_type="repository", title="Evidence Code", domain_slugs=["agent"]),
            Asset(asset_type="paper", title="Evidence Different", domain_slugs=["agent-extra"]),
        ]
    )
    service.session.commit()
    statements = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(service.session.bind, "before_cursor_execute", record_statement)
    try:
        result = service.search_assets(
            "Evidence", domain="agent", asset_type="paper", source_type="original", limit=1
        )
    finally:
        event.remove(service.session.bind, "before_cursor_execute", record_statement)
    assert result["total"] == 1 and result["items"][0]["asset_id"] == str(asset.id)
    assert len(statements) == 2
    assert "LIMIT" in statements[-1]
    assert service.search_assets("10.123/evidence")["total"] == 1
    assert service.search_assets("%")["total"] == 0
    with pytest.raises(KnowledgeError):
        service.search_assets("", limit=21)


def test_extraction_activity_detects_stale_text_without_registered_hash(library):
    service, asset, rep, _, _ = library
    digest = rep.sha256
    rep.sha256 = None
    activity = Activity(
        asset_id=asset.id,
        representation_id=rep.id,
        activity_type="extraction",
        tool_name="extraction-pipeline",
        status="succeeded",
        output_digest=text_hash("paper.pdf" + digest),
    )
    service.session.add(activity)
    service.session.flush()
    chunks = service.session.query(DocumentChunk).all()
    for chunk in chunks:
        chunk.activity_id = activity.id
        chunk.anchor = {**chunk.anchor, "source_path": "paper.pdf"}
    service.session.flush()
    assert service.read_evidence(asset.id)["text"].startswith("first")
    activity.output_digest = "0" * 64
    with pytest.raises(KnowledgeError) as error:
        service.read_evidence(asset.id)
    assert error.value.code == "stale_extraction"


def test_http_routes_validate_limits_and_return_json(library):
    service, asset, rep, *_ = library
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:", database_require_migrations=False
    )
    database = DatabaseManager(settings, engine=service.session.bind, require_migrations=False)
    app = FastAPI()
    app.state.settings = settings
    app.state.database = database
    app.state.source_registry = service.registry
    app.include_router(knowledge._v1.router)
    install_problem_handlers(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/knowledge/search", params={"limit": 21}).status_code == 422
        assert (
            client.get(
                f"/api/v1/knowledge/papers/{asset.id}/evidence", params={"max_chars": 16001}
            ).status_code
            == 422
        )
        evidence = client.get(
            f"/api/v1/knowledge/papers/{asset.id}/evidence",
            params={"representation_id": str(rep.id), "page_end": 1},
        )
        assert evidence.status_code == 200 and evidence.json()["text"] == "first page text"
        assert client.get("/api/v1/knowledge/status").json()["read_only"] is True
        ready = client.get("/api/v1/knowledge/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"] == ["database_query", "source_configuration"]
        assert ready.json()["remote_index_verified"] is False
        assert client.get(f"/api/v1/knowledge/papers/{uuid4()}").status_code == 404
        assert client.post("/api/v1/knowledge/search").status_code == 405
        assert (
            client.get("/api/v1/knowledge/search", params={"keywords": ["x"] * 9}).status_code
            == 422
        )
        assert (
            client.get("/api/v1/knowledge/status", params={"domain": "unknown-domain"}).status_code
            == 422
        )


def test_indexed_domain_mapping_and_query_contract(library):
    service, asset, *_ = library
    asset.title = "Efficient Video Diffusion"
    asset.domain_slugs = ["generation"]
    asset.attributes = {"category": "video_core"}
    service.session.commit()
    service.taxonomy_path = PROJECT_ROOT / "config/taxonomy.yaml"
    index = InMemoryKeywordIndex()
    index.upsert(
        [
            SearchDocument(
                document_id="asset:" + str(asset.id),
                asset_id=str(asset.id),
                kind="asset",
                title=asset.title,
                text="Video diffusion",
                metadata={"domains": ["generation"]},
            )
        ]
    )
    service.keyword_search = lambda: KeywordSearchService({"assets": index})
    result = service.search_assets(
        "寻找视频扩散方法",
        domain="视频生成",
        keywords=["Video", "Diffusion"],
        constraints=["无需重新训练"],
    )
    assert result["degraded"] is False
    assert result["items"][0]["asset_id"] == str(asset.id)
    assert result["items"][0]["domains"] == ["video-generation"]
    assert result["items"][0]["source_domains"] == ["generation"]
    assert result["items"][0]["constraints_need_evidence_verification"] is True
    assert result["query_plan"]["conditions"]
    assert service.search_assets("Video", domain="图像编辑")["items"] == []
    assert not service.session.dirty


def test_source_configuration_failure_is_not_metadata_degradation(library):
    service, *_ = library

    def invalid_config():
        raise SourceConfigurationError("invalid fixture")

    service.keyword_search = invalid_config
    with pytest.raises(SourceConfigurationError):
        service.search_assets("Evidence")


def test_query_variants_run_concurrently_but_database_stays_on_request_thread(library, monkeypatch):
    service, asset, *_ = library
    request_thread = threading.get_ident()
    started_threads = set()
    lock = threading.Lock()
    all_started = threading.Event()

    class ConcurrentIndex(InMemoryKeywordIndex):
        def search(self, query, **kwargs):
            with lock:
                started_threads.add(threading.get_ident())
                if len(started_threads) == 3:
                    all_started.set()
            assert all_started.wait(5), "Independent query variants must start together"
            return super().search(query, **kwargs)

    index = ConcurrentIndex()
    index.upsert(
        [
            SearchDocument(
                document_id="asset:" + str(asset.id),
                asset_id=str(asset.id),
                kind="asset",
                title=asset.title,
                text="alpha beta gamma",
            )
        ]
    )
    service.keyword_search = lambda: KeywordSearchService({"assets": index})
    monkeypatch.setattr(
        "packages.knowledge.service.plan_query",
        lambda *a, **kw: QueryPlan(
            queries=("alpha", "beta", "gamma"),
            original="example",
            concept_groups=(),
            conditions=(),
            named_terms=(),
            unmapped=(),
        ),
    )

    def sql_thread(conn, cursor, statement, parameters, context, executemany):
        assert threading.get_ident() == request_thread

    event.listen(service.session.bind, "before_cursor_execute", sql_thread)
    try:
        result = service.search_assets("example")
    finally:
        event.remove(service.session.bind, "before_cursor_execute", sql_thread)
    assert result["degraded"] is False and result["total"] == 1
    assert len(started_threads) == 3 and request_thread not in started_threads


def test_http_bad_source_configuration_is_503(library, monkeypatch):
    service, *_ = library
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:", database_require_migrations=False
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.database = DatabaseManager(
        settings, engine=service.session.bind, require_migrations=False
    )
    app.state.source_registry = service.registry
    app.include_router(knowledge._v1.router)
    install_problem_handlers(app)

    def invalid_config(*args, **kwargs):
        raise SourceConfigurationError("invalid fixture")

    monkeypatch.setattr(service.registry, "refresh_if_changed", invalid_config)
    with TestClient(app) as client:
        response = client.get("/api/v1/knowledge/status")
    assert response.status_code == 503
    assert response.json()["code"] == "source_configuration_invalid"


def test_fulltext_index_keeps_evidence_and_filters_disabled_sources(library):
    service, asset, rep, source, _ = library
    chunks = service.session.query(DocumentChunk).order_by(DocumentChunk.chunk_index).all()
    index = InMemoryKeywordIndex()
    index.upsert(
        [
            SearchDocument(
                document_id="chunk:" + str(chunks[1].id),
                asset_id=str(asset.id),
                kind="document",
                title=asset.title,
                text=chunks[1].text_content,
                metadata={"representation_id": str(rep.id), "anchor": chunks[1].anchor},
            )
        ]
    )
    service.keyword_search = lambda: KeywordSearchService({"documents": index})
    result = service.search_assets("second page")
    assert result["backend"] == "meilisearch" and result["degraded"] is False
    assert result["items"][0]["snippet"] == "second page text"
    assert result["items"][0]["anchor"]["page"] == 2
    assert result["items"][0]["source_type"] == "original"
    assert result["items"][0]["evidence_id"].startswith("chunk:")
    assert result["items"][0]["requires_read_evidence"] is True
    assert service.search_assets("second page", source_type="analysis")["items"] == []
    source.enabled = False
    service.session.flush()
    assert service.search_assets("second page")["items"] == []


def test_keyword_failure_is_explicit_metadata_degradation(library):
    service, *_ = library

    def unavailable():
        raise ConnectionError("not reachable")

    service.keyword_search = unavailable
    result = service.search_assets("Evidence")
    assert result["degraded"] is True
    assert result["backend"] == "postgresql_metadata"
    assert result["errors"] == ["keyword_runtime:ConnectionError"]
    assert len(result["items"]) == 1


@pytest.mark.parametrize("representation_type", ["html", "markdown"])
def test_historical_original_role_webpage_is_analysis(library, representation_type):
    service, asset, rep, _, tmp_path = library
    path = tmp_path / ("analysis.html" if representation_type == "html" else "analysis.md")
    path.write_text("中文解读页面", encoding="utf-8")
    rep.representation_type = representation_type
    rep.role = "original"
    rep.path_ref = path.name
    rep.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    for chunk in service.session.query(DocumentChunk).all():
        service.session.delete(chunk)
    chunk = DocumentChunk(
        asset_id=asset.id,
        representation_id=rep.id,
        chunk_index=0,
        chunk_kind=representation_type,
        text_content="中文解读页面",
        content_hash=text_hash("中文解读页面"),
        parser_name="html",
        parser_version="1",
        anchor_kind="html_heading",
        anchor={"kind": "html", "selector": "#analysis"},
    )
    service.session.add(chunk)
    service.session.flush()
    assert representation_source_type(rep) == "analysis"
    assert service.get_paper(asset.id)["representations"][0]["source_type"] == "analysis"
    with pytest.raises(KnowledgeError) as error:
        service.read_evidence(asset.id, representation_id=rep.id, source_type="original")
    assert error.value.code == "source_type_mismatch"
    analysis = service.read_evidence(asset.id, source_type="analysis")
    assert analysis["text"] == "中文解读页面" and analysis["anchor_precision"] == "document"
    assert service.search_assets("Evidence", source_type="original")["total"] == 0
    assert service.search_assets("Evidence", source_type="analysis")["total"] == 1
    index = InMemoryKeywordIndex()
    index.upsert(
        [
            SearchDocument(
                document_id="chunk:" + str(chunk.id),
                asset_id=str(asset.id),
                kind="document",
                title=asset.title,
                text=chunk.text_content,
                metadata={"representation_id": str(rep.id), "anchor": chunk.anchor},
            )
        ]
    )
    service.keyword_search = lambda: KeywordSearchService({"documents": index})
    assert service.search_assets("中文解读", source_type="original")["items"] == []
    result = service.search_assets("中文解读", source_type="analysis")
    assert result["items"][0]["source_type"] == "analysis"
