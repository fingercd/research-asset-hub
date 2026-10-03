from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.api.serialization import serialize_representation
from apps.asset_gateway.main import create_app as create_gateway
from apps.asset_gateway.registry import SourceRegistry
from apps.asset_gateway.security import create_gateway_token
from apps.worker.handlers import RuntimeHandlers, cancel_retired_pending_jobs, dispatch_outbox_once
from packages.cli.config import LoadedSource
from packages.domain.models import (
    Activity,
    Asset,
    AuditEvent,
    BackupRun,
    Base,
    DocumentChunk,
    FieldResolution,
    Job,
    OutboxEvent,
    Representation,
    Source,
    SourceRetirement,
)
from packages.domain.schemas import NormalizedRecord
from packages.domain.scope_policy import DomainScopePolicy
from packages.domain.serving_scope import (
    RetiredRepresentationError,
    active_representation_clause,
    representation_serving_status,
)
from packages.extraction.models import ExtractionChunk, ExtractionResult, ExtractionStatus
from packages.indexing import (
    IndexBuilder,
    InMemoryKeywordIndex,
    PostgresIndexCatalog,
    RemoteIndexSuite,
)
from packages.indexing.builder import KEYWORD_INDEX_NAMES
from packages.ingestion import formal


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "wrong.pdf"
    pdf.write_bytes(b"frozen original PDF bytes")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"items":[{"id":"paper"}]}', encoding="utf-8")
    source = LoadedSource(
        source_id="serving-test",
        label="Serving test",
        domain="agent",
        adapter="generic_manifest",
        root=tmp_path,
        manifest=manifest,
        expected_records=1,
        read_only=True,
        raw={
            "id": "serving-test",
            "root": str(tmp_path),
            "manifest": "manifest.json",
            "adapter": "generic_manifest",
        },
    )
    record = NormalizedRecord.model_validate(
        {
            "source_id": "serving-test",
            "source_record_id": "paper",
            "adapter_name": "test",
            "asset": {"title": "Serving Scope Paper", "domain_slugs": ["agent"]},
            "representations": [
                {
                    "representation_type": "pdf",
                    "role": "primary",
                    "root_key": "source",
                    "path_ref": "wrong.pdf",
                }
            ],
            "raw": {"id": "paper"},
        }
    )
    monkeypatch.setattr(formal, "_load_all_records", lambda _sources: [record])
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
        gateway_secret="g" * 64,
        host_workspace_root=tmp_path,
    )
    settings.sources_config_path.write_text(
        "schema_version: 1\nworkspace_root: .\nsources: []\n", "utf-8"
    )
    settings.taxonomy_config_path.write_text(
        "schema_version: 1\ndomains:\n  - id: agent\n    label: Agent\n", "utf-8"
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)

    def reimport() -> None:
        with database.session() as session:
            formal.run_formal_import(
                session,
                [source],
                known_domain_slugs={"agent"},
                domain_scope_policy=DomainScopePolicy(),
            )

    reimport()
    with database.session() as session:
        rep = session.scalar(select(Representation))
        rep_id, asset_id, digest = rep.id, rep.asset_id, rep.sha256
        statement = serialize_representation(rep)
        session.add(
            DocumentChunk(
                asset_id=asset_id,
                representation_id=rep_id,
                chunk_index=0,
                chunk_kind="document",
                text_content="original frozen evidence",
                anchor={"page": 1},
                content_hash="a" * 64,
                parser_name="test",
                parser_version="1",
            )
        )
    registry = SourceRegistry({("serving-test", "source"): tmp_path})
    app = create_app(settings, database=database)
    app.state.source_registry = registry
    gateway_app = create_gateway(settings, database=database, registry=registry)
    with TestClient(app) as client, TestClient(gateway_app) as gateway:
        yield {
            "client": client,
            "gateway": gateway,
            "database": database,
            "settings": settings,
            "rep_id": rep_id,
            "asset_id": asset_id,
            "sha256": digest,
            "registry": registry,
            "statement": statement,
            "reimport": reimport,
            "manifest": manifest,
            "pdf": pdf,
        }
    engine.dispose()


def change(
    library: dict[str, Any],
    *,
    excluded: bool = True,
    key: str = "exclude",
    etag: str | None = None,
    **fields: Any,
):
    client = library["client"]
    url = f"/api/v1/representations/{library['rep_id']}/serving"
    if etag is None:
        etag = client.get(url).headers["ETag"]
    return client.patch(
        url,
        headers={"If-Match": etag, "Idempotency-Key": key},
        json={
            "excluded": excluded,
            "expected_sha256": library["sha256"],
            "reason": "The PDF belongs to another paper.",
            **fields,
        },
    )


def test_exclusion_audit_idempotency_versions_and_explicit_restore(library):
    client, database = library["client"], library["database"]
    url = f"/api/v1/representations/{library['rep_id']}/serving"
    before = client.get(url)
    assert before.headers["ETag"] == 'W/"0"'
    result = change(library, etag=before.headers["ETag"])
    assert result.status_code == 200, result.text
    assert result.json()["excluded"] is True and result.json()["active"] is False
    control_id = result.json()["resolution_id"]
    # Reserved serving policy cannot be cleared through asset-metadata overrides.
    for method, suffix, payload in [
        ("get", "", None),
        ("delete", "", None),
        ("patch", "", {"reason": "bypass"}),
        ("post", "/restore", None),
    ]:
        rejected = client.request(
            method,
            f"/api/v1/overrides/{control_id}{suffix}",
            json=payload,
            headers={"If-Match": result.headers["ETag"]},
        )
        assert rejected.status_code == 404
    replay = change(library, etag=before.headers["ETag"])
    assert replay.json() == result.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert change(library, excluded=False).status_code == 409
    assert change(library, key="stale-write", etag=before.headers["ETag"]).status_code == 412
    with database.session() as session:
        rep = session.get(Representation, library["rep_id"])
        assert serialize_representation(rep) == library["statement"]
        assert (
            len(
                session.scalars(
                    select(FieldResolution).where(FieldResolution.entity_type == "representation")
                ).all()
            )
            == 1
        )
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.entity_type == "representation_serving")
        )
        assert audit.before["excluded"] is False and audit.after["excluded"] is True
        assert audit.after["reason"]
    restored = change(library, excluded=False, key="restore", etag=result.headers["ETag"])
    assert restored.status_code == 200
    assert restored.json()["active"] is True
    assert restored.json()["row_version"] > result.json()["row_version"]
    with database.session() as session:
        assert (
            len(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.entity_type == "representation_serving")
                ).all()
            )
            == 2
        )
        assert (
            serialize_representation(session.get(Representation, library["rep_id"]))
            == library["statement"]
        )


@pytest.mark.parametrize(
    "headers,fields,status",
    [
        ({"Idempotency-Key": "k"}, {}, 428),
        ({"If-Match": 'W/"0"'}, {}, 428),
        ({"Idempotency-Key": "k", "If-Match": "*"}, {}, 428),
        ({"Idempotency-Key": "k", "If-Match": 'W/"0"'}, {"reason": "  "}, 422),
        ({"Idempotency-Key": "k", "If-Match": 'W/"0"'}, {"expected_sha256": "0" * 64}, 409),
    ],
)
def test_exclusion_requires_condition_key_reason_and_matching_fingerprint(
    library, headers, fields, status
):
    result = library["client"].patch(
        f"/api/v1/representations/{library['rep_id']}/serving",
        headers=headers,
        json={
            "excluded": True,
            "reason": "checked identity",
            "expected_sha256": library["sha256"],
            **fields,
        },
    )
    assert result.status_code == status
    with library["database"].session() as session:
        assert (
            session.scalar(
                select(FieldResolution).where(FieldResolution.entity_type == "representation")
            )
            is None
        )


def test_exclusion_survives_real_reimport_without_mutating_frozen_evidence(library):
    manifest_before, pdf_before = library["manifest"].read_bytes(), library["pdf"].read_bytes()
    with library["database"].session() as session:
        frozen_before = dict(session.scalar(select(Source)).config)
    assert change(library).status_code == 200
    library["reimport"]()
    with library["database"].session() as session:
        rep = session.get(Representation, library["rep_id"])
        assert rep.availability == "available" and rep.sha256 == library["sha256"]
        assert representation_serving_status(session, rep).active is False
        assert (
            session.scalar(select(Representation.id).where(active_representation_clause())) is None
        )
        assert session.scalar(select(Source)).config == frozen_before
    assert library["manifest"].read_bytes() == manifest_before
    assert library["pdf"].read_bytes() == pdf_before


def test_exclusion_blocks_original_gateway_figures_worker_and_scoped_catalog(library):
    client, database, rep_id, asset_id = (
        library[k] for k in ("client", "database", "rep_id", "asset_id")
    )
    read_url = f"/api/v1/knowledge/papers/{asset_id}/evidence?representation_id={rep_id}"
    assert client.get(read_url).status_code == 200
    cache = library["settings"].host_workspace_root / "outputs/knowledge-pilot/v1" / str(asset_id)
    cache.mkdir(parents=True)
    (cache / "figures.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "asset_id": str(asset_id),
                "representation_id": str(rep_id),
                "source_hash": library["sha256"],
                "figures": [{"figure_id": "excluded-test-fig", "page": 1, "figure_number": "1"}],
            }
        ),
        "utf-8",
    )
    assert client.get("/api/v1/knowledge/figures/excluded-test-fig").status_code == 200
    assert change(library).status_code == 200
    for path, method in [
        (read_url, "get"),
        (f"/api/v1/representations/{rep_id}/open-actions", "post"),
        ("/api/v1/knowledge/figures/excluded-test-fig", "get"),
        ("/api/v1/knowledge/figures/excluded-test-fig/image", "get"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 410, response.text
        assert response.json()["code"] == "representation_excluded"
    token = create_gateway_token(
        library["settings"].gateway_secret, rep_id, ttl_seconds=60, nonce="test"
    )
    gateway_result = library["gateway"].get(f"/session/{rep_id}?token={token}")
    assert (
        gateway_result.status_code == 410
        and gateway_result.json()["code"] == "representation_excluded"
    )
    # A grant issued before exclusion must not bypass the current decision.
    signed = library["gateway"].get(f"/session-assets/{token}/original/{rep_id}")
    assert signed.status_code == 410 and signed.json()["code"] == "representation_excluded"
    assert client.get("/api/v1/knowledge/search?source_type=original").json()["total"] == 0
    assert client.get("/api/v1/knowledge/search").json()["total"] == 1
    assert client.get(f"/api/v1/knowledge/papers/{asset_id}").json()["representations"] == []
    audit_rep = client.get(
        f"/api/v1/assets/{asset_id}/representations?include_retired=true"
    ).json()["items"][0]
    assert audit_rep["excluded"] is True and audit_rep["retired"] is False
    assert audit_rep["availability"] == "available" and audit_rep["path_ref"] is None
    handlers = RuntimeHandlers(database, library["settings"], library["registry"])
    assert (
        handlers.extract_representation({"representation_id": str(rep_id)})["status"]
        == "skipped_excluded"
    )
    with pytest.raises(RetiredRepresentationError) as caught:
        handlers._representation_paths(rep_id)
    assert caught.value.code == "representation_excluded"
    with database.session() as session:
        session.add(
            Job(job_type="extract_representation", parameters={"representation_id": str(rep_id)})
        )
    with database.session() as session:
        assert cancel_retired_pending_jobs(session) == 1
        job = session.scalar(select(Job).where(Job.job_type == "extract_representation"))
        assert job.status == "cancelled" and job.error["type"] == "RepresentationExcluded"
        catalog = PostgresIndexCatalog().load(session, asset_ids=(asset_id,)).keyword_documents
        assert catalog["documents"] == () and catalog["code"] == ()
        assert catalog["assets"][0].metadata["has_pdf"] is False
        assert session.scalar(select(DocumentChunk)) is not None
        # Archive scope retains the original chunks as provenance.
        archived = (
            PostgresIndexCatalog()
            .load(session, scope="all", asset_ids=(asset_id,))
            .keyword_documents
        )
        assert len(archived["documents"]) == 1
    assert change(library, excluded=False, key="restore").status_code == 200
    assert client.get(read_url).status_code == 200
    assert library["gateway"].get(f"/session-assets/{token}/original/{rep_id}").status_code == 200


def test_serving_audit_outbox_enqueues_scoped_cleanup(library):
    assert change(library).status_code == 200
    with library["database"].session() as session:
        for event in session.scalars(select(OutboxEvent)).all():
            if event.aggregate_type != "representation_serving":
                event.status = "published"
    assert dispatch_outbox_once(library["database"], worker_id="serving-test")
    with library["database"].session() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "delete_asset_index")).all()
        assert len(jobs) == 1
        assert jobs[0].parameters == {"asset_id": str(library["asset_id"])}


def test_scoped_refresh_removes_wrong_chunks_and_restores_only_by_audited_change(library):
    indexes = {name: InMemoryKeywordIndex() for name in KEYWORD_INDEX_NAMES}

    class MemoryAdmin:
        def document_count(self, name):
            return len(indexes[name].documents)

        def iter_asset_document_id_batches(self, name, *, asset_ids, batch_size):
            owners = {str(value) for value in asset_ids}
            ids = sorted(
                doc.document_id
                for doc in indexes[name].documents.values()
                if doc.asset_id in owners
            )
            for offset in range(0, len(ids), batch_size):
                yield ids[offset : offset + batch_size]

    suite = SimpleNamespace(
        keyword_indexes=indexes,
        meili_admin=MemoryAdmin(),
        meili_transport=SimpleNamespace(wait_for_tasks=True, flush_pending_tasks=lambda: None),
    )
    builder = IndexBuilder(cast(RemoteIndexSuite, suite))
    with library["database"].session() as session:
        builder.refresh_keywords(
            session, asset_ids=(library["asset_id"],), index_names=KEYWORD_INDEX_NAMES
        )
    assert len(indexes["documents"].documents) == 1
    assert next(iter(indexes["assets"].documents.values())).metadata["has_pdf"] is True
    assert change(library).status_code == 200
    with library["database"].session() as session:
        report = builder.refresh_keywords(
            session, asset_ids=(library["asset_id"],), index_names=KEYWORD_INDEX_NAMES
        )
        assert report.stale_keyword_documents_removed["documents"] == 1
        assert session.scalar(select(DocumentChunk)) is not None
    assert len(indexes["documents"].documents) == 0
    assert next(iter(indexes["assets"].documents.values())).metadata["has_pdf"] is False
    assert change(library, excluded=False, key="restore").status_code == 200
    with library["database"].session() as session:
        builder.refresh_keywords(
            session, asset_ids=(library["asset_id"],), index_names=KEYWORD_INDEX_NAMES
        )
    assert len(indexes["documents"].documents) == 1
    assert next(iter(indexes["assets"].documents.values())).metadata["has_pdf"] is True


def test_code_endpoints_share_the_exclusion_gate(library):
    with library["database"].session() as session:
        original = session.get(Representation, library["rep_id"])
        code = Representation(
            asset_id=original.asset_id,
            source_id=original.source_id,
            representation_type="source_code",
            role="official",
            path_ref="model.py",
            sha256="a" * 64,
            availability="available",
        )
        session.add(code)
        session.flush()
        code_id = code.id
    url = f"/api/v1/representations/{code_id}/serving"
    version = library["client"].get(url).headers["ETag"]
    response = library["client"].patch(
        url,
        headers={"If-Match": version, "Idempotency-Key": "code"},
        json={"excluded": True, "expected_sha256": "a" * 64, "reason": "wrong implementation"},
    )
    assert response.status_code == 200
    for endpoint in [
        f"/api/v1/representations/{code_id}/code/tree",
        f"/api/v1/representations/{code_id}/code/file?path=model.py",
    ]:
        rejected = library["client"].get(endpoint)
        assert rejected.status_code == 410 and rejected.json()["code"] == "representation_excluded"


def test_retirement_still_takes_precedence_over_representation_control(library):
    assert change(library).status_code == 200
    with library["database"].session() as session:
        rep = session.get(Representation, library["rep_id"])
        backup = BackupRun(destination_label="serving-test")
        session.add(backup)
        session.flush()
        retirement = SourceRetirement(
            source_id=rep.source_id,
            backup_id=backup.id,
            payload={},
            payload_digest="b" * 64,
            frozen_manifest_sha256="a" * 64,
            frozen_source_config_sha256="a" * 64,
            record_set_digest="a" * 64,
            representation_set_digest="a" * 64,
            mount_set_digest="a" * 64,
            collection_digest="a" * 64,
            signature_algorithm="hmac-sha256",
            signer_key_id="test",
            signature="test-signature",
            applied_by="pytest",
        )
        session.add(retirement)
    assert change(library, excluded=False, key="restore").json()["active"] is False
    response = library["client"].post(f"/api/v1/representations/{library['rep_id']}/open-actions")
    assert response.status_code == 410 and response.json()["code"] == "representation_retired"


def test_successor_links_only_to_a_different_active_representation_of_same_asset(library):
    client = library["client"]
    assert change(library, successor_representation_id=str(library["rep_id"])).status_code == 409
    with library["database"].session() as session:
        other = Asset(asset_type="paper", title="Another paper")
        session.add(other)
        session.flush()
        rep = Representation(
            asset_id=other.id,
            representation_type="pdf",
            role="primary",
            path_ref="other.pdf",
            sha256="c" * 64,
            availability="available",
        )
        session.add(rep)
        session.flush()
        foreign = rep.id
    assert change(library, successor_representation_id=str(foreign)).status_code == 409
    with library["database"].session() as session:
        current = session.get(Representation, foreign)
        current.asset_id = library["asset_id"]
    accepted = change(library, successor_representation_id=str(foreign))
    assert accepted.status_code == 200, accepted.text
    denied = client.post(f"/api/v1/representations/{library['rep_id']}/open-actions")
    assert denied.headers["X-Successor-Representation-ID"] == str(foreign)


def test_exclusion_during_parsing_preserves_old_chunks_and_skips_commit(library):
    class ParserWithConcurrentDecision:
        def extract(self, path, *, representation_id):
            assert change(library).status_code == 200
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=library["sha256"],
                extractor="test",
                extractor_version="1",
                status=ExtractionStatus.SUCCEEDED,
                chunks=(
                    ExtractionChunk(
                        chunk_id="new",
                        representation_id=representation_id,
                        ordinal=0,
                        text="must not overwrite frozen history",
                        content_hash="b" * 64,
                        extractor="test",
                        extractor_version="1",
                        anchor={"kind": "pdf", "page": 1},
                    ),
                ),
            )

    handlers = RuntimeHandlers(
        library["database"],
        library["settings"],
        library["registry"],
        extraction_pipeline=ParserWithConcurrentDecision(),
    )
    result = handlers.extract_representation({"representation_id": str(library["rep_id"])})
    assert result["status"] == "skipped_excluded"
    with library["database"].session() as session:
        chunks = session.scalars(select(DocumentChunk)).all()
        assert len(chunks) == 1 and chunks[0].text_content == "original frozen evidence"
        activity = session.scalar(select(Activity).where(Activity.activity_type == "extraction"))
        assert activity.status == "skipped" and activity.result["status"] == "skipped_excluded"
        assert session.scalar(select(Job).where(Job.job_type == "index_representation")) is None
