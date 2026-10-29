from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.asset_gateway.registry import SourceRegistry
from packages.cli.config import load_sources
from packages.domain.models import (
    Asset,
    Base,
    ExternalIdentifierModel,
    Job,
    ReadingState,
    Representation,
    Source,
)
from packages.domain.workflow_models import PackageExport, PackageImport
from packages.ingestion.formal import run_formal_import
from packages.ingestion.source_integrity import verify_source_integrity
from packages.managed_sources.service import load_managed_sources
from packages.workflows.adapter import DomainPackageAdapter
from packages.workflows.packages import PackageError, export_package, inspect_package


@pytest.fixture
def package_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "user-data"))
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
        yield client, database, settings, tmp_path
    engine.dispose()


def seed(database: DatabaseManager, root: Path) -> tuple[str, SourceRegistry]:
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / "paper.pdf"
    content = b"%PDF-1.4\nresearch attachment\n"
    pdf.write_bytes(content)
    code = root / "code"
    code.mkdir()
    (code / "main.py").write_text("print('research')\n", encoding="utf-8")
    (code / ".env").write_text("API_KEY=never-export\n", encoding="utf-8")
    with database.session() as session:
        source = Source(
            alias="fixture-source",
            label="Fixture",
            adapter="generic_manifest",
            root_path=str(root),
            read_only=True,
        )
        asset = Asset(
            asset_type="paper",
            title="Graph Research",
            year=2026,
            domain_slugs=["agent"],
            source_urls=["https://user:password@example.org/paper?token=private"],
            attributes={"private_path": "C:/Private/secret", "api_token": "never-export"},
        )
        session.add_all([source, asset])
        session.flush()
        session.add_all(
            [
                ExternalIdentifierModel(
                    asset_id=asset.id,
                    scheme="doi",
                    value="10.1234/graph",
                    canonical="10.1234/graph",
                    is_identity=True,
                ),
                Representation(
                    asset_id=asset.id,
                    source_id=source.id,
                    representation_type="pdf",
                    role="original",
                    path_ref="paper.pdf",
                    root_key="pdf",
                    sha256=hashlib.sha256(content).hexdigest(),
                    availability="available",
                ),
                Representation(
                    asset_id=asset.id,
                    source_id=source.id,
                    representation_type="repository",
                    role="official",
                    path_ref="code",
                    root_key="repository",
                    materialization_kind="directory",
                    availability="available",
                ),
                ReadingState(asset_id=asset.id, status="read"),
            ]
        )
        return str(asset.id), SourceRegistry(
            {("fixture-source", "pdf"): root, ("fixture-source", "repository"): root}
        )


def build(
    client: TestClient,
    database: DatabaseManager,
    settings: ApiSettings,
    registry: SourceRegistry,
    asset_id: str,
    *,
    user_state: bool = False,
) -> tuple[str, bytes]:
    response = client.post(
        "/api/v1/exports", json={"asset_ids": [asset_id], "include_user_state": user_state}
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert client.get(f"/api/v1/exports/{job_id}/download").status_code == 409
    with database.session() as session:
        parameters = dict(session.get(Job, UUID(job_id)).parameters)
    result = export_package(database, settings, registry, {**parameters, "_job_id": job_id})
    assert result["status"] == "succeeded"
    with database.session() as session:
        session.get(Job, UUID(job_id)).status = "succeeded"
    downloaded = client.get(f"/api/v1/exports/{job_id}/download")
    assert downloaded.status_code == 200, downloaded.text
    return job_id, downloaded.content


def test_export_contains_registered_files_with_path_free_manifest(package_runtime):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    job_id, data = build(client, database, settings, registry, asset_id)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["representations"]) == 2
        assert sum(len(row["files"]) for row in manifest["representations"]) == 2
        assert "user_state" not in manifest
        encoded = json.dumps(manifest)
        assert (
            "private_path" not in encoded
            and "never-export" not in encoded
            and str(tmp_path) not in encoded
        )
        assert manifest["assets"][0]["source_urls"] == ["https://example.org/paper"]
        assert any(row["reason"] == "sensitive_name" for row in manifest["skipped_files"])
        for row in manifest["representations"]:
            for file in row["files"]:
                assert hashlib.sha256(archive.read(file["path"])).hexdigest() == file["sha256"]
    with database.session() as session:
        artifact = session.scalar(select(PackageExport).where(PackageExport.job_id == UUID(job_id)))
        path = tmp_path / "user-data" / "library" / "packages" / artifact.relative_path
    assert inspect_package(path).schema_version == 1
    assert (tmp_path / "source" / "code" / ".env").read_text() == "API_KEY=never-export\n"


def test_formal_source_loading_includes_registered_packages_and_reimport_is_idempotent(
    package_runtime,
):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, registry, asset_id, user_state=True)
    uploaded = client.post("/api/v1/imports/packages?filename=reimport.zip", content=data)
    assert uploaded.status_code == 201, uploaded.text
    package_id = uploaded.json()["id"]
    committed = client.post(
        f"/api/v1/imports/packages/{package_id}/commit",
        json={"include_user_state": True},
        headers={"If-Match": uploaded.headers["etag"]},
    )
    assert committed.status_code == 200, committed.text
    data_dir = tmp_path / "user-data"
    declarations = load_managed_sources(data_dir)
    sources = load_sources(settings.sources_config_path, data_dir=data_dir)
    assert {source.source_id for source in sources} == {row["id"] for row in declarations}
    assert len(sources) == 1 and sources[0].adapter == "domain_package"
    with database.session() as session:
        before_assets = session.scalar(select(func.count()).select_from(Asset))
        before_representations = session.scalar(select(func.count()).select_from(Representation))
        reading = session.scalar(select(ReadingState))
        reading.status = "reading"
        frozen_manifest = Path(sources[0].manifest).read_bytes()
        first = run_formal_import(session, sources, known_domain_slugs={"agent"})
        second = run_formal_import(session, sources, known_domain_slugs={"agent"})
        for report in (first, second):
            assert report["counters"]["paper_assets_created"] == 0
            assert report["counters"]["representations_created"] == 0
        assert session.scalar(select(func.count()).select_from(Asset)) == before_assets
        assert (
            session.scalar(select(func.count()).select_from(Representation))
            == before_representations
        )
        assert session.get(Asset, UUID(asset_id)).id == UUID(asset_id)
        assert session.scalar(select(ReadingState)).status == "reading"
        assert Path(sources[0].manifest).read_bytes() == frozen_manifest


def test_package_import_reuses_identity_and_preserves_reading_state(package_runtime):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, registry, asset_id, user_state=True)
    # Simulate the local reader changing status after this archive was created.
    with database.session() as session:
        session.scalar(select(ReadingState)).status = "reading"
    uploaded = client.post(
        "/api/v1/imports/packages?filename=fixture.zip",
        content=data,
        headers={"Content-Type": "application/zip"},
    )
    assert uploaded.status_code == 201, uploaded.text
    package_id = uploaded.json()["id"]
    assert uploaded.json()["preview"]["assets"][0]["action"] == "reuse"
    committed = client.post(
        f"/api/v1/imports/packages/{package_id}/commit",
        json={"include_user_state": True},
        headers={"If-Match": uploaded.headers["etag"]},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["assets_created"] == 0
    assert committed.json()["assets_reused"] == 1
    with database.session() as session:
        assert session.scalar(select(ReadingState)).status == "reading"
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(PackageImport)) == 1
    retry = client.post("/api/v1/imports/packages?filename=again.zip", content=data)
    assert retry.json()["id"] == package_id and retry.json()["status"] == "committed"
    assert (
        client.post(f"/api/v1/imports/packages/{package_id}/commit", json={}).json()
        == committed.json()
    )
    # Re-zipping identical payloads changes ZIP bytes but remains the same semantic import.
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as old,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as new,
    ):
        for name in old.namelist():
            new.writestr(name, old.read(name))
    duplicate = client.post("/api/v1/imports/packages", content=buffer.getvalue())
    assert duplicate.json()["id"] == package_id
    declarations = load_managed_sources(tmp_path / "user-data")
    assert len(declarations) == 1
    assert len(DomainPackageAdapter(declarations[0]).load()) == 1


def test_import_into_empty_database_creates_snapshots_and_followup_jobs(package_runtime):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, registry, asset_id)
    # Clear only the isolated in-memory fixture database to represent another installation.
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    uploaded = client.post("/api/v1/imports/packages?filename=fixture.zip", content=data)
    assert uploaded.status_code == 201, uploaded.text
    package_id = uploaded.json()["id"]
    committed = client.post(
        f"/api/v1/imports/packages/{package_id}/commit",
        json={},
        headers={"If-Match": uploaded.headers["etag"]},
    )
    assert committed.status_code == 200, committed.text
    assert (
        committed.json()["assets_created"] == 1 and committed.json()["representations_created"] == 2
    )
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ReadingState)) == 0
        assert {job.job_type for job in session.scalars(select(Job))} == {
            "index_asset",
            "extract_representation",
        }
        managed_source = session.scalar(select(Source))
        assert managed_source.read_only is True and managed_source.config["_frozen_manifest_sha256"]
        representation = session.scalar(
            select(Representation).where(Representation.representation_type == "pdf")
        )
        managed = (
            Path(managed_source.config["asset_roots"]["attachments"]) / representation.path_ref
        )
        assert managed.read_bytes() == (tmp_path / "source" / "paper.pdf").read_bytes()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "attachments/../escape",
        "C:/escape",
        "attachments/a\\b",
        "attachments/a//b",
        "attachments/CON",
        "/absolute",
    ],
)
def test_zip_traversal_and_windows_aliases_rejected(tmp_path: Path, name: str):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"bad")
    with pytest.raises(PackageError):
        inspect_package(path)


def test_symlink_and_zip_bomb_rejected(tmp_path: Path):
    path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("attachments/symlink")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, b"../../private")
    with pytest.raises(PackageError, match="普通文件"):
        inspect_package(path)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("attachments/bomb", b"0" * (2 * 1024**2))
    with pytest.raises(PackageError, match="膨胀"):
        inspect_package(path)


def test_attachment_checksum_failure_creates_no_import_record(package_runtime):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, registry, asset_id)
    changed = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(changed, "w") as archive:
        replaced = False
        for name in original.namelist():
            content = original.read(name)
            if name.startswith("attachments/") and not replaced:
                content = bytes([content[0] ^ 1]) + content[1:]
                replaced = True
            archive.writestr(name, content)
    response = client.post("/api/v1/imports/packages", content=changed.getvalue())
    assert response.status_code == 422 and "SHA-256" in response.json()["detail"]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PackageImport)) == 0


def test_weak_identity_requires_choice_and_keeps_existing_reader_state(package_runtime):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, registry, asset_id, user_state=True)
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        local = Asset(asset_type="paper", title="Graph Research", year=2026, domain_slugs=["agent"])
        session.add(local)
        session.flush()
        local_id = str(local.id)
        session.add(ReadingState(asset_id=local.id, status="reading"))
    uploaded = client.post("/api/v1/imports/packages", content=data)
    import_id = uploaded.json()["id"]
    assert uploaded.json()["preview"]["assets"][0]["action"] == "needs_review"
    assert uploaded.json()["preview"]["assets"][0]["candidates"][0]["id"] == local_id
    url = f"/api/v1/imports/packages/{import_id}/commit"
    headers = {"If-Match": uploaded.headers["etag"]}
    assert client.post(url, json={}, headers=headers).status_code == 409
    committed = client.post(
        url,
        json={"identity_mapping": {asset_id: local_id}, "include_user_state": True},
        headers=headers,
    )
    assert committed.status_code == 200, committed.text
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(ReadingState)).status == "reading"


def test_failed_commit_retries_without_rewriting_immutable_source(package_runtime, monkeypatch):
    client, database, settings, tmp_path = package_runtime
    asset_id, registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, registry, asset_id)
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    uploaded = client.post("/api/v1/imports/packages", content=data)
    import_id = uploaded.json()["id"]
    from packages.workflows import packages as service

    original_audit = service.audit
    failed = False

    def fail_once(session, entity, entity_type, before, after, request_id, **kwargs):
        nonlocal failed
        if entity_type == "representation" and not failed:
            failed = True
            raise PackageError("模拟事务失败", 409)
        return original_audit(session, entity, entity_type, before, after, request_id, **kwargs)

    monkeypatch.setattr(service, "audit", fail_once)
    url = f"/api/v1/imports/packages/{import_id}/commit"
    headers = {"If-Match": uploaded.headers["etag"]}
    assert client.post(url, json={}, headers=headers).status_code == 409
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 0
        assert session.get(PackageImport, UUID(import_id)).status == "preview"
    declaration = load_managed_sources(tmp_path / "user-data")[0]
    source_manifest = Path(declaration["root"]) / "manifest.json"
    before = source_manifest.read_bytes()
    retry = client.post(url, json={}, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["assets_created"] == 1 and source_manifest.read_bytes() == before


@pytest.mark.parametrize("changed_kind", ["file", "directory"])
def test_imported_package_passes_actual_source_integrity_and_detects_changes(
    package_runtime, changed_kind
):
    client, database, settings, tmp_path = package_runtime
    asset_id, source_registry = seed(database, tmp_path / "source")
    _, data = build(client, database, settings, source_registry, asset_id)
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    uploaded = client.post("/api/v1/imports/packages?filename=integrity.zip", content=data)
    assert uploaded.status_code == 201, uploaded.text
    import_id = uploaded.json()["id"]
    committed = client.post(
        f"/api/v1/imports/packages/{import_id}/commit",
        json={},
        headers={"If-Match": uploaded.headers["etag"]},
    )
    assert committed.status_code == 200, committed.text
    data_dir = tmp_path / "user-data"
    registry = SourceRegistry.load(settings.sources_config_path, data_dir=data_dir)
    with database.session() as session:
        baseline = verify_source_integrity(session, registry).to_dict()
        assert baseline["status"] == "passed", baseline
        assert baseline["counts"]["files_verified"] == 1
        assert baseline["counts"]["directories_verified"] == 1
        representation = session.scalar(
            select(Representation).where(Representation.materialization_kind == changed_kind)
        )
        source = session.get(Source, representation.source_id)
        location = registry.resolve(source.alias, representation.root_key, representation.path_ref)
        actual_stat = location.stat()
        assert representation.attributes["_source_size"] == actual_stat.st_size
        assert representation.attributes["_source_mtime_ns"] == actual_stat.st_mtime_ns
        sources = load_sources(settings.sources_config_path, data_dir=data_dir)
        report = run_formal_import(session, sources, known_domain_slugs={"agent"})
        assert report["counters"]["representations_created"] == 0
        assert verify_source_integrity(session, registry).to_dict()["status"] == "passed"
        if changed_kind == "directory":
            assert representation.attributes["package_content_tree_digest"]
    changed = location if changed_kind == "file" else location / "main.py"
    changed.write_bytes(changed.read_bytes() + b"changed\n")
    with database.session() as session:
        report = verify_source_integrity(session, registry).to_dict()
        assert report["status"] == "failed"
        mismatch = next(
            item
            for item in report["mismatches"]
            if item["representation_id"] == str(representation.id)
        )
        assert (
            "sha256_mismatch" if changed_kind == "file" else "tree_digest_mismatch"
        ) in mismatch["reasons"]
