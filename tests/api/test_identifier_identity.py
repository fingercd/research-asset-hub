from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.asset_gateway.registry import SourceRegistry
from packages.cli.config import LoadedSource
from packages.domain.identifier_policy import rejected_identity_keys
from packages.domain.models import (
    AuditEvent,
    Base,
    ExternalIdentifierModel,
    FieldClaim,
    FieldResolution,
    OutboxEvent,
    SourceRecord,
    SourceRecordVersion,
)
from packages.domain.schemas import NormalizedRecord
from packages.domain.scope_policy import DomainScopePolicy
from packages.indexing import PostgresIndexCatalog
from packages.ingestion import formal
from packages.ingestion.dry_run import run_dry_run
from packages.ingestion.reconciliation import reconcile_records, record_identity_keys
from packages.knowledge.service import KnowledgeService


@pytest.fixture(params=["1112.6209", "2011.12729"])
def library(tmp_path: Path, monkeypatch, request):
    value = request.param
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"frozen":"original declaration"}', encoding="utf-8")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"frozen original PDF bytes")

    def source(alias):
        return LoadedSource(
            source_id=alias,
            label=alias,
            domain="agent",
            adapter="generic_manifest",
            root=tmp_path,
            manifest=manifest,
            expected_records=1,
            read_only=True,
            raw={"id": alias, "root": str(tmp_path), "manifest": "manifest.json"},
        )

    def record(alias, title):
        return NormalizedRecord.model_validate(
            {
                "source_id": alias,
                "source_record_id": "paper",
                "adapter_name": "test",
                "asset": {
                    "title": title,
                    "domain_slugs": ["agent"],
                    "external_identifiers": [{"scheme": "arxiv", "value": value}],
                },
                "representations": [
                    {
                        "representation_type": "pdf",
                        "role": "primary",
                        "root_key": "source",
                        "path_ref": "paper.pdf",
                    }
                ],
                "raw": {"arxiv_id": value},
                "source_fields": {"arxiv_id": value},
            }
        )

    records = [record("original", "Paper with incorrect identity")]
    sources = [source("original")]
    monkeypatch.setattr(formal, "_load_all_records", lambda _sources: records)
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///" + (tmp_path / "library.db").as_posix(),
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

    def reimport():
        with database.session() as session:
            return formal.run_formal_import(
                session,
                sources,
                known_domain_slugs={"agent"},
                domain_scope_policy=DomainScopePolicy(),
            )

    reimport()
    with database.session() as session:
        identifier = session.scalar(select(ExternalIdentifierModel))
        identifier_id, asset_id = identifier.id, identifier.asset_id
        frozen_version = session.scalar(select(SourceRecordVersion))
        frozen = (frozen_version.raw_data, frozen_version.source_fields, frozen_version.raw_digest)
        claims_count = session.scalar(select(func.count()).select_from(FieldClaim))
    with TestClient(create_app(settings, database=database)) as client:
        yield {
            "client": client,
            "database": database,
            "identifier_id": identifier_id,
            "asset_id": asset_id,
            "value": value,
            "reimport": reimport,
            "records": records,
            "sources": sources,
            "record": record,
            "source": source,
            "frozen": frozen,
            "claims_count": claims_count,
            "manifest": manifest,
            "pdf": pdf,
            "registry": SourceRegistry({("original", "source"): tmp_path}),
            "cache_root": tmp_path / "cache",
            "hashes": [hashlib.sha256(p.read_bytes()).hexdigest() for p in [manifest, pdf]],
        }
    engine.dispose()


def change(library, *, key="reject", rejected=True, etag=None, **changes):
    client = library["client"]
    url = f"/api/v1/external-identifiers/{library['identifier_id']}/identity"
    if etag is None:
        etag = client.get(url).headers["ETag"]
    return client.patch(
        url,
        headers={"If-Match": etag, "Idempotency-Key": key},
        json={
            "rejected": rejected,
            "expected_asset_id": str(library["asset_id"]),
            "expected_scheme": "arxiv",
            "expected_canonical": library["value"],
            "reason": "Verified original paper belongs to a different work.",
            **changes,
        },
    )


def test_audit_idempotency_conditional_reject_and_restore(library):
    client = library["client"]
    url = f"/api/v1/external-identifiers/{library['identifier_id']}/identity"
    original = client.get(url)
    assert original.headers["ETag"] == 'W/"0"'
    first = change(library, etag=original.headers["ETag"])
    assert first.status_code == 200, first.text
    assert first.json()["is_identity"] is False and first.json()["rejected"] is True
    assert first.json()["source_records"][0]["source_record_id"] == "paper"
    control_url = f"/api/v1/overrides/{first.json()['resolution_id']}"
    assert client.get(control_url).status_code == 404
    assert client.patch(control_url, json={"override_value": False}).status_code == 404
    assert client.delete(control_url).status_code == 404
    assert client.post(control_url + "/restore").status_code == 404
    replay = change(library, etag=original.headers["ETag"])
    assert replay.status_code == 200 and replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert change(library, key="stale", etag=original.headers["ETag"]).status_code == 412
    assert change(library, reason="Different payload").status_code == 409
    assert change(library, key="wrong-value", expected_canonical="9999.99999").status_code == 409
    assert change(library, key="blank", reason="  ").status_code == 422
    assert client.patch(url, json={}).status_code == 422
    restored = change(library, key="restore", rejected=False)
    assert restored.status_code == 200 and restored.json()["is_identity"] is True
    with library["database"].session() as session:
        assert (
            len(
                session.scalars(
                    select(FieldResolution).where(
                        FieldResolution.entity_type == "external_identifier"
                    )
                ).all()
            )
            == 1
        )
        audit = session.scalars(
            select(AuditEvent).where(
                AuditEvent.after["external_identifier_id"].as_string()
                == str(library["identifier_id"])
            )
        ).all()
        assert len(audit) == 2
        assert audit[0].before["is_identity"] is True
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "asset.identifier-identity-updated")
            )
            == 2
        )


def test_original_claims_survive_reimport_and_real_paper_can_own_same_identifier(
    library, monkeypatch
):
    assert change(library).status_code == 200
    library["reimport"]()
    with library["database"].session() as session:
        identifier = session.get(ExternalIdentifierModel, library["identifier_id"])
        assert not identifier.is_identity
        keys = rejected_identity_keys(session)
        assert record_identity_keys(library["records"][0], rejected_keys=keys)["strong"] == []
        assert record_identity_keys(library["records"][0])["strong"] == [
            f"arxiv:{library['value']}"
        ]
        version = session.scalar(select(SourceRecordVersion))
        assert (version.raw_data, version.source_fields, version.raw_digest) == library["frozen"]
        assert session.scalar(select(func.count()).select_from(SourceRecordVersion)) == 1
        assert (
            session.scalar(select(func.count()).select_from(FieldClaim)) == library["claims_count"]
        )
        assert (
            KnowledgeService(session, library["registry"], library["cache_root"]).search_assets(
                library["value"]
            )["total"]
            == 0
        )
        catalog = PostgresIndexCatalog().load(session)
        assert all(
            library["value"] not in doc.exact_terms for doc in catalog.keyword_documents["assets"]
        )
    library["records"].append(library["record"]("real", "A different authentic paper"))
    library["sources"].append(library["source"]("real"))
    monkeypatch.setattr(
        "packages.ingestion.dry_run.DEFAULT_REGISTRY.create",
        lambda _adapter, config, **_kwargs: SimpleNamespace(
            load=lambda: [item for item in library["records"] if item.source_id == config.source_id]
        ),
    )
    with library["database"].session() as session:
        keys = rejected_identity_keys(session)
        assert reconcile_records(library["records"], rejected_keys=keys)["auto_merge_groups"] == []
        original_report = run_dry_run(library["sources"], known_domain_slugs={"agent"})
        corrected_report = run_dry_run(
            library["sources"], known_domain_slugs={"agent"}, rejected_identity_keys=keys
        )
        assert len(original_report["reconciliation"]["auto_merge_groups"]) == 1
        assert corrected_report["reconciliation"]["auto_merge_groups"] == []
        assert (
            original_report["reconciliation"]["record_fingerprints"]
            == (corrected_report["reconciliation"]["record_fingerprints"])
        )
    library["reimport"]()
    library["reimport"]()
    with library["database"].session() as session:
        identifiers = session.scalars(select(ExternalIdentifierModel)).all()
        assert len(identifiers) == 2
        assert len([item for item in identifiers if item.is_identity]) == 1
        real = next(item for item in identifiers if item.is_identity)
        assert real.asset_id != library["asset_id"]
        found = KnowledgeService(session, library["registry"], library["cache_root"]).search_assets(
            library["value"]
        )
        assert found["total"] == 1 and found["items"][0]["asset_id"] == str(real.asset_id)
        catalog = PostgresIndexCatalog().load(session)
        found_docs = [
            doc
            for doc in catalog.keyword_documents["assets"]
            if library["value"] in doc.exact_terms
        ]
        assert len(found_docs) == 1 and found_docs[0].asset_id == str(real.asset_id)
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 2
    assert change(library, key="restore-occupied", rejected=False).status_code == 409
    assert library["hashes"] == [
        hashlib.sha256(p.read_bytes()).hexdigest() for p in [library["manifest"], library["pdf"]]
    ]


def test_same_etag_competing_decisions_do_not_overwrite(library):
    # Both writers observe version zero. The second request must not overwrite
    # the first audited decision, even with a different idempotency key.
    assert change(library, key="writer-a", etag='W/"0"').status_code == 200
    assert change(library, key="writer-b", etag='W/"0"', rejected=False).status_code == 412
    with library["database"].session() as session:
        assert not session.get(ExternalIdentifierModel, library["identifier_id"]).is_identity


def test_concurrent_requests_use_version_compare_and_swap(library, monkeypatch):
    from apps.api import routes_v1

    assert change(library).status_code == 200
    barrier = Barrier(2)
    original_check = routes_v1._check_if_match

    def both_read_same_version(*args, **kwargs):
        barrier.wait(timeout=10)
        return original_check(*args, **kwargs)

    monkeypatch.setattr(routes_v1, "_check_if_match", both_read_same_version)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(change, library, key="concurrent-restore", rejected=False, etag='W/"1"'),
            workers.submit(change, library, key="concurrent-reject", rejected=True, etag='W/"1"'),
        ]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result.status_code for result in results) == [200, 412]
    winning = next(result.json() for result in results if result.status_code == 200)
    with library["database"].session() as session:
        identifier = session.get(ExternalIdentifierModel, library["identifier_id"])
        assert identifier.is_identity == winning["is_identity"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "asset.identifier-identity-updated")
            )
            == 2
        )


def test_identity_patch_requires_concrete_version_and_idempotency_key(library):
    url = f"/api/v1/external-identifiers/{library['identifier_id']}/identity"
    payload = {
        "rejected": True,
        "expected_asset_id": str(library["asset_id"]),
        "expected_scheme": "arxiv",
        "expected_canonical": library["value"],
        "reason": "Verified mismatch",
    }
    client = library["client"]
    assert client.patch(url, json=payload).status_code == 428
    assert client.patch(url, json=payload, headers={"Idempotency-Key": "a"}).status_code == 428
    assert (
        client.patch(
            url, json=payload, headers={"Idempotency-Key": "a", "If-Match": "*"}
        ).status_code
        == 428
    )
