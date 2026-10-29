from __future__ import annotations

import hashlib
import os
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import func, select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.cli.config import load_sources
from packages.domain.models import Asset, Base, Job, Representation, Source, SourceRecordVersion
from packages.domain.product_models import IntakeDraft
from packages.domain_catalog.service import list_domain_definitions
from packages.ingestion.formal import _verify_frozen_source_contract, run_formal_import
from packages.ingestion.source_integrity import verify_source_integrity
from packages.knowledge.domains import normalize_domain
from packages.managed_sources.service import register_managed_source


@pytest.fixture
def runtime(tmp_path):
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        data_dir=tmp_path / "user-data",
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
        gateway_secret="k" * 64,
        host_workspace_root=tmp_path,
        intake_reference_roots=[tmp_path / "permanent"],
    )
    settings.sources_config_path.write_text("workspace_root: .\nsources: []\n", encoding="utf-8")
    settings.taxonomy_config_path.write_text(
        "domains:\n  - id: agent\n    label: 智能体\n"
        "  - id: hidden\n    label: 历史领域\n    hidden_from_catalog: true\n",
        encoding="utf-8",
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with TestClient(create_app(settings, database=database)) as client:
        yield client, database, settings
    engine.dispose()


def pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def draft(client, **values):
    response = client.post(
        "/api/v1/intake/drafts", json={"domain": "agent", "title": "Minimal Paper", **values}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, draft_id, content=None):
    response = client.put(
        f"/api/v1/intake/drafts/{draft_id}/pdf?filename=paper.pdf",
        content=content or pdf_bytes(),
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def commit(client, draft_id, **values):
    response = client.post(
        f"/api/v1/intake/drafts/{draft_id}/commit", json={"action": "create", **values}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_pdf_and_title_only_commit_is_idempotent_and_freezes_source(runtime):
    client, database, settings = runtime
    identifier = draft(client)
    content = pdf_bytes()
    upload(client, identifier, content)
    result = commit(client, identifier)
    assert commit(client, identifier) == result
    assert client.delete(f"/api/v1/intake/drafts/{identifier}").status_code == 409
    path = settings.data_dir / "library" / "documents" / identifier / "paper.pdf"
    assert path.read_bytes() == content
    assert not (settings.data_dir / "library" / "staging" / identifier).exists()
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(Representation)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRecordVersion)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        source = session.scalar(select(Source))
        assert (
            source.config["_frozen_manifest_sha256"]
            == hashlib.sha256((Path(source.root_path) / "manifest.json").read_bytes()).hexdigest()
        )
        asset = session.get(Asset, UUID(result["asset_id"]))
        assert asset.authors == [] and asset.abstract is None
    registry = SourceRegistry.load(settings.sources_config_path, data_dir=settings.data_dir)
    assert registry.resolve("intake-" + UUID(identifier).hex, "pdf", "paper.pdf") == path


@pytest.mark.parametrize("storage_mode", ["managed", "reference"])
@pytest.mark.parametrize("action", ["create", "attach"])
def test_published_pdf_records_real_stat_and_passes_source_integrity(runtime, storage_mode, action):
    client, database, settings = runtime
    original_id = draft(client)
    upload(client, original_id)
    original = commit(client, original_id)
    original_path = settings.data_dir / "library" / "documents" / original_id / "paper.pdf"
    original_content, original_mtime = original_path.read_bytes(), original_path.stat().st_mtime_ns
    identifier = draft(client, storage_mode=storage_mode)
    if storage_mode == "managed":
        uploaded = upload(client, identifier)
        staged = (
            settings.data_dir / "library" / "staging" / identifier / f"{uploaded['pdf_sha256']}.pdf"
        )
        # A copied permanent file must use its actual published stat, not the
        # staging file's metadata or an invented timestamp.
        os.utime(staged, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        published = settings.data_dir / "library" / "documents" / identifier / "paper.pdf"
    else:
        published = settings.intake_reference_roots[0] / "original.pdf"
        published.parent.mkdir()
        published.write_bytes(pdf_bytes())
        response = client.patch(
            f"/api/v1/intake/drafts/{identifier}", json={"reference_path": str(published)}
        )
        assert response.status_code == 200, response.text
    kwargs = {"action": action}
    if action == "attach":
        kwargs["asset_id"] = original["asset_id"]
    commit(client, identifier, **kwargs)
    registry = SourceRegistry.load(settings.sources_config_path, data_dir=settings.data_dir)
    sources = load_sources(settings.sources_config_path, data_dir=settings.data_dir)
    with database.session() as session:
        report = verify_source_integrity(session, registry).to_dict()
        assert report["status"] == "passed", report
        assert report["counts"]["files_verified"] == 2
        source = session.scalar(
            select(Source).where(Source.alias == "intake-" + UUID(identifier).hex)
        )
        representation = session.scalar(
            select(Representation).where(Representation.source_id == source.id)
        )
        actual = published.stat()
        assert representation.attributes["_source_size"] == actual.st_size
        assert representation.attributes["_source_mtime_ns"] == actual.st_mtime_ns
        for loaded in sources:
            model = session.scalar(select(Source).where(Source.alias == loaded.source_id))
            manifest_hash, config_hash = _verify_frozen_source_contract(model, loaded)
            assert manifest_hash == model.config["_frozen_manifest_sha256"]
            assert config_hash == model.config["_frozen_source_config_sha256"]
    assert original_path.read_bytes() == original_content
    assert original_path.stat().st_mtime_ns == original_mtime
    with database.session() as session:
        # Verify that a real later change is still detected; capturing the stat
        # baseline must not turn the integrity gate into an unconditional pass.
        published.write_bytes(published.read_bytes() + b"\n% changed after registration\n")
        changed = verify_source_integrity(session, registry).to_dict()
        assert changed["status"] == "failed"
        assert any(
            "sha256_mismatch" in row["reasons"] and "size_mismatch" in row["reasons"]
            for row in changed["mismatches"]
        )


def test_new_empty_domain_is_usable_and_slug_survives_rename(runtime):
    client, database, settings = runtime
    created = client.post(
        "/api/v1/domains", json={"label": "数学推理", "aliases": ["Math Reasoning"]}
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    patch = client.patch(
        f"/api/v1/domains/{slug}", json={"label": "形式推理", "description": "新领域"}
    )
    assert patch.status_code == 200 and patch.json()["slug"] == slug
    with database.session() as session:
        rows = list_domain_definitions(session, settings)
        assert slug in {row["slug"] for row in rows}
        assert "hidden" not in {row["slug"] for row in rows}
        assert (
            normalize_domain("Math Reasoning", settings.taxonomy_config_path, session=session)
            == slug
        )
    identifier = draft(client, domain=slug)
    upload(client, identifier)
    commit(client, identifier)


def test_strong_duplicates_require_choice_and_attach_preserves_original(runtime):
    client, database, settings = runtime
    first = draft(client, doi="10.1234/example")
    upload(client, first)
    original = commit(client, first)
    original_path = settings.data_dir / "library" / "documents" / first / "paper.pdf"
    original_content = original_path.read_bytes()
    second = draft(client, title="User translated title", doi="https://doi.org/10.1234/EXAMPLE")
    upload(client, second)
    candidates = client.get(f"/api/v1/intake/drafts/{second}/preview").json()["duplicates"]
    assert candidates[0]["asset_id"] == original["asset_id"]
    assert {"doi", "sha256"} <= set(candidates[0]["reasons"])
    rejected = client.post(f"/api/v1/intake/drafts/{second}/commit", json={"action": "create"})
    assert rejected.status_code == 409
    attached = commit(client, second, action="attach", asset_id=original["asset_id"])
    assert attached["asset_id"] == original["asset_id"]
    assert original_path.read_bytes() == original_content
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(Representation)) == 2
        assert session.get(Asset, UUID(original["asset_id"])).title == "Minimal Paper"


@pytest.mark.parametrize("content", [b"not a PDF", b"%PDF-1.7\nbroken\n%%EOF"])
def test_corrupt_pdf_does_not_publish_or_change_draft(runtime, content):
    client, _, settings = runtime
    identifier = draft(client)
    response = client.put(
        f"/api/v1/intake/drafts/{identifier}/pdf",
        content=content,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 422
    assert client.get(f"/api/v1/intake/drafts/{identifier}").json()["pdf_uploaded"] is False
    assert not list((settings.data_dir / "library" / "staging" / identifier).iterdir())


def test_cancel_only_removes_owned_staging_and_keeps_reference(runtime):
    client, _, settings = runtime
    identifier = draft(client)
    upload(client, identifier)
    assert client.delete(f"/api/v1/intake/drafts/{identifier}").status_code == 200
    assert not (settings.data_dir / "library" / "staging" / identifier).exists()
    reference = settings.intake_reference_roots[0] / "original.pdf"
    reference.parent.mkdir()
    reference.write_bytes(pdf_bytes())
    ref_id = draft(client, storage_mode="reference")
    response = client.patch(
        f"/api/v1/intake/drafts/{ref_id}", json={"reference_path": str(reference)}
    )
    assert response.status_code == 200, response.text
    assert client.delete(f"/api/v1/intake/drafts/{ref_id}").status_code == 200
    assert reference.read_bytes() == pdf_bytes()


def test_reference_requires_approved_root_and_valid_pdf(runtime, tmp_path):
    client, _, _ = runtime
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    identifier = draft(client, storage_mode="reference")
    result = client.patch(
        f"/api/v1/intake/drafts/{identifier}", json={"reference_path": str(outside)}
    )
    assert result.status_code == 422


def test_reference_commit_preserves_original_and_detects_changed_input(runtime):
    client, _, settings = runtime
    reference = settings.intake_reference_roots[0] / "selected.pdf"
    reference.parent.mkdir()
    content = pdf_bytes()
    reference.write_bytes(content)
    identifier = draft(client, storage_mode="reference")
    assert (
        client.patch(
            f"/api/v1/intake/drafts/{identifier}", json={"reference_path": str(reference)}
        ).status_code
        == 200
    )
    reference.write_bytes(content + b"\n%changed\n")
    changed = client.post(f"/api/v1/intake/drafts/{identifier}/commit", json={"action": "create"})
    assert changed.status_code == 409
    reference.write_bytes(content)
    commit(client, identifier)
    assert reference.read_bytes() == content
    assert not (settings.data_dir / "library" / "documents" / identifier).exists()
    registry = SourceRegistry.load(settings.sources_config_path, data_dir=settings.data_dir)
    assert registry.resolve("intake-" + UUID(identifier).hex, "pdf", "selected.pdf") == reference


def test_blank_domain_and_invalid_identifiers_fail_before_persistence(runtime):
    client, _, _ = runtime
    assert client.post("/api/v1/domains", json={"label": "   "}).status_code == 422
    assert client.patch("/api/v1/domains/agent", json={"label": None}).status_code == 422
    assert (
        client.post(
            "/api/v1/intake/drafts", json={"domain": "agent", "doi": "not-a-doi"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/intake/drafts", json={"domain": "agent", "title": "bad\x00title"}
        ).status_code
        == 422
    )


def test_failed_database_commit_can_retry_without_overwriting_frozen_pdf(runtime, monkeypatch):
    client, database, settings = runtime
    identifier = draft(client)
    upload(client, identifier)
    import apps.api.routes.intake as route

    complete = route.complete_commit

    def fail_after_write(*args, **kwargs):
        complete(*args, **kwargs)
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(route, "complete_commit", fail_after_write)
    # TestClient rethrows unhandled exceptions, matching the API's actual rollback path.
    with pytest.raises(RuntimeError):
        client.post(f"/api/v1/intake/drafts/{identifier}/commit", json={"action": "create"})
    path = settings.data_dir / "library" / "documents" / identifier / "paper.pdf"
    before = path.read_bytes()
    with database.session() as session:
        assert session.get(IntakeDraft, UUID(identifier)).status == "committing"
        assert session.scalar(select(func.count()).select_from(Asset)) == 0
    assert (
        client.patch(f"/api/v1/intake/drafts/{identifier}", json={"title": "Changed"}).status_code
        == 409
    )
    monkeypatch.setattr(route, "complete_commit", complete)
    commit(client, identifier)
    assert path.read_bytes() == before


def test_registry_hot_refresh_and_append_only_managed_manifest(runtime):
    _, _, settings = runtime
    registry = SourceRegistry.load(settings.sources_config_path, data_dir=settings.data_dir)
    root = settings.data_dir / "library" / "documents" / "sample"
    source = {
        "id": "sample-source",
        "label": "sample",
        "domain": "agent",
        "adapter": "generic_manifest",
        "asset_roots": {"pdf": str(root)},
    }
    manifest = {"items": [{"id": "sample", "title": "sample"}]}
    result = register_managed_source(settings.data_dir, source, manifest)
    assert register_managed_source(settings.data_dir, source, manifest) == result
    assert registry.resolve("sample-source", "pdf", "paper.pdf") == root / "paper.pdf"
    with pytest.raises(ValueError):
        register_managed_source(settings.data_dir, source, {"items": [{"id": "different"}]})
    for path in ("../x", "C:/x", "paper//x", "./paper.pdf", "/paper.pdf", "a\x00.pdf"):
        with pytest.raises(UnsafeAssetPath):
            registry.resolve("sample-source", "pdf", path)


def test_attach_domain_survives_original_source_reimport_and_uses_normal_identifier_keys(runtime):
    client, database, settings = runtime
    first = draft(client, doi="10.1234/stable")
    upload(client, first)
    original = commit(client, first)
    new_domain = client.post("/api/v1/domains", json={"label": "New domain"}).json()["slug"]
    second = draft(
        client, domain=new_domain, title="Optional attachment metadata", doi="10.1234/stable"
    )
    upload(client, second)
    commit(client, second, action="attach", asset_id=original["asset_id"])
    sources = load_sources(settings.sources_config_path, data_dir=settings.data_dir)
    with database.session() as session:
        # Import only the old source, reproducing a pipeline that does not see
        # the later manually attached representation.
        old = next(source for source in sources if source.source_id == "intake-" + UUID(first).hex)
        report = run_formal_import(session, [old], known_domain_slugs={"agent"})
        asset = session.get(Asset, UUID(original["asset_id"]))
        assert asset.domain_slugs == ["agent", new_domain]
        assert asset.title == "Minimal Paper"
        assert report["counters"]["paper_assets_created"] == 0
        from packages.domain.models import ExternalIdentifierModel, FieldResolution

        assert session.scalar(select(ExternalIdentifierModel.canonical)) == "10.1234/stable"
        assert session.scalar(
            select(FieldResolution).where(FieldResolution.field_path == "/domain_slugs")
        ).override_value == ["agent", new_domain]
        assert session.scalar(select(func.count()).select_from(Representation)) == 2


def test_new_database_domain_is_valid_for_formal_import_even_with_legacy_known_set(runtime):
    client, database, settings = runtime
    domain = client.post("/api/v1/domains", json={"label": "Fresh Domain"}).json()["slug"]
    identifier = draft(client, domain=domain, doi="10.1234/fresh")
    upload(client, identifier)
    result = commit(client, identifier)
    sources = load_sources(settings.sources_config_path, data_dir=settings.data_dir)
    with database.session() as session:
        report = run_formal_import(session, sources, known_domain_slugs={"agent"})
        assert report["counters"]["paper_assets_created"] == 0
        assert session.get(Asset, UUID(result["asset_id"])).domain_slugs == [domain]


def test_generic_formal_loader_does_not_load_repository_snapshot_contracts(runtime):
    _, _, settings = runtime
    register_managed_source(
        settings.data_dir,
        {
            "id": "code-opaque",
            "label": "code",
            "domain": "agent",
            "adapter": "managed_repository",
            "asset_roots": {"repository": str(settings.data_dir / "library" / "repositories")},
        },
        {"schema": "snapshot-v1", "sha": "a" * 40},
    )
    assert load_sources(settings.sources_config_path, data_dir=settings.data_dir) == []
