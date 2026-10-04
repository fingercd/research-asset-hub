from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from apps.api.routes import repository_snapshots  # noqa: F401
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.backup.release_report import (
    LegacyReference,
    ReleaseReportInputs,
    build_release_report,
    collect_database_snapshot,
)
from packages.cli.config import load_sources
from packages.cli.inspection import build_inventory_report, source_preflight
from packages.cli.main import main as cli_main
from packages.domain.models import (
    Asset,
    AuditEvent,
    BackupRun,
    Base,
    FieldResolution,
    Job,
    Relation,
    RelationEvidenceModel,
    RepositoryProfile,
    Representation,
    Source,
    SourceRetirement,
)
from packages.domain.serving_scope import RetiredRepresentationError
from packages.ingestion.source_integrity import verify_source_integrity
from packages.repository_snapshots import download
from packages.repository_snapshots import service as snapshot_service
from packages.repository_snapshots.download import (
    SnapshotCancelled,
    SnapshotError,
    extract_archive,
    public_repository,
    resolve_commit,
)
from packages.repository_snapshots.service import link_code, queue_snapshot, run_repository_snapshot

SHA = "a" * 40
SHA2 = "b" * 40
HTTP_CLIENT = httpx.Client


@pytest.mark.parametrize("source_kind", ["local", "snapshot"])
@pytest.mark.parametrize("tamper", ["manifest", "config", "missing_inventory"])
def test_native_source_inventory_covers_real_ledger_and_frozen_release_gate(
    runtime, tmp_path, monkeypatch, source_kind, tamper
):
    database, settings, registry, _ = runtime
    if source_kind == "local":
        selected = tmp_path / "selected"
        selected.mkdir()
        (selected / "main.py").write_text("print('source')\n", encoding="utf-8")
        create_link(runtime, url=None, local_directory=str(selected))
    else:
        result = create_link(runtime, download=True)
        mocked_client(monkeypatch)
        run_repository_snapshot(
            database,
            settings,
            registry,
            {
                "repository_asset_id": result["repository_asset_id"],
                "_job_id": result["job_id"],
            },
        )
    assert load_sources(settings.sources_config_path, data_dir=settings.data_dir) == []
    sources = load_sources(
        settings.sources_config_path, data_dir=settings.data_dir, include_platform=True
    )
    assert len(sources) == 1 and sources[0].adapter == "managed_repository"
    preflight = source_preflight(sources[0])
    assert preflight["errors"] == [] and preflight["record_count"] == 0
    assert preflight["record_count_matches"] is True
    clean = build_inventory_report(sources, operational_registry=registry)
    assert clean["status"] == "passed" and clean["totals"]["source_record_count"] == 0
    monkeypatch.setenv("HUB_DATA_DIR", str(settings.data_dir))
    captured = {}

    def capture_inventory(report, name):
        captured["report"] = report
        path = tmp_path / name
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    monkeypatch.setattr("packages.cli.main.write_report", capture_inventory)
    assert (
        cli_main(["--config", str(settings.sources_config_path), "inventory", "--all", "--json"])
        == 0
    )
    assert {item["id"] for item in captured["report"]["sources"]} == {sources[0].source_id}
    with database.session() as session:
        ledger = collect_database_snapshot(session)
    assert {item["id"] for item in clean["sources"]} == {
        item["alias"] for item in ledger["sources"]["items"]
    }

    def gate(inventory):
        report = build_release_report(
            ReleaseReportInputs(
                database=ledger,
                reports={
                    "inventory_current": inventory,
                    "inventory_baseline": clean,
                },
            ),
            legacy=LegacyReference(domain_count=0),
        )
        return next(item for item in report["gates"] if item["id"] == "source-fingerprints")

    accepted = gate(clean)
    assert accepted["status"] == "pass", accepted
    observed = accepted["observed"]
    assert observed["missing_inventory_sources"] == []
    assert observed["missing_frozen_config_fingerprints"] == []
    before_manifest = sources[0].manifest.read_bytes()
    if tamper == "manifest":
        document = json.loads(before_manifest)
        document["source_record_id"] += ":changed"
        sources[0].manifest.write_text(json.dumps(document), encoding="utf-8")
        changed = build_inventory_report(sources, operational_registry=registry)
        field = "frozen_fingerprint_mismatches"
    elif tamper == "config":
        overlay = settings.data_dir / "config" / "managed-sources.yaml"
        document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        document["sources"][0]["label"] += " changed"
        overlay.write_text(yaml.safe_dump(document), encoding="utf-8")
        changed = build_inventory_report(
            load_sources(
                settings.sources_config_path, data_dir=settings.data_dir, include_platform=True
            ),
            operational_registry=registry,
        )
        assert sources[0].manifest.read_bytes() == before_manifest
        field = "frozen_config_fingerprint_mismatches"
    else:
        changed = build_inventory_report([], operational_registry=registry)
        field = "missing_inventory_sources"
    rejected = gate(changed)
    assert rejected["status"] == "fail", rejected
    assert rejected["observed"][field]


@pytest.fixture
def runtime(tmp_path):
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        data_dir=tmp_path / "data",
        sources_config_path=tmp_path / "sources.yaml",
    )
    settings.sources_config_path.write_text("sources: []\n", encoding="utf-8")
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    registry = SourceRegistry.load(settings.sources_config_path, data_dir=settings.data_dir)
    with database.session() as session:
        paper = Asset(asset_type="paper", title="Paper", domain_slugs=["agent"])
        session.add(paper)
        session.flush()
        paper_id = paper.id
    yield database, settings, registry, paper_id
    engine.dispose()


def zip_bytes(items=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in items or [
            ("repo/README.md", b"paper implementation"),
            ("repo/main.py", b"print('never executed')"),
            ("repo/weights/model.pt", b"weights"),
            ("repo/.gitmodules", b"[submodule]"),
        ]:
            member = zipfile.ZipInfo(name)
            member.filename = name
            archive.writestr(member, content)
    return stream.getvalue()


def create_link(runtime, **overrides):
    database, settings, registry, paper_id = runtime
    values = dict(
        paper_id=paper_id,
        url="https://github.com/example/implementation.git",
        existing_asset_id=None,
        local_directory=None,
        kind="official",
        download=False,
        revision=None,
        data_dir=settings.data_dir,
    )
    values.update(overrides)
    with database.session() as session:
        return link_code(session, **values)


def mocked_client(monkeypatch, *, commit=SHA, archive_status=200, on_archive=None):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "api.github.com":
            requested = request.url.path.rsplit("/", 1)[-1]
            resolved = requested if len(requested) == 40 else commit
            return httpx.Response(200, json={"sha": resolved})
        if on_archive:
            on_archive()
        return httpx.Response(archive_status, content=zip_bytes())

    monkeypatch.setattr(
        "packages.repository_snapshots.service.httpx.Client",
        lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kwargs),
    )
    return requests


def test_shared_repo_and_evidence_are_idempotent(runtime):
    first = create_link(runtime)
    assert create_link(runtime) == first
    database, settings, registry, _ = runtime
    with database.session() as session:
        second_paper = Asset(asset_type="paper", title="Another paper", domain_slugs=["other"])
        session.add(second_paper)
        session.flush()
        second = link_code(
            session,
            paper_id=second_paper.id,
            url="https://github.com/example/implementation/",
            existing_asset_id=None,
            local_directory=None,
            kind="reproduction",
            download=False,
            revision=None,
            data_dir=settings.data_dir,
        )
        assert first["repository_asset_id"] == second["repository_asset_id"]
        assert session.scalar(select(func.count()).select_from(RepositoryProfile)) == 1
        assert session.scalar(select(func.count()).select_from(Relation)) == 2
        assert session.scalar(select(func.count()).select_from(RelationEvidenceModel)) == 2
        evidence = session.scalar(select(RelationEvidenceModel))
        assert evidence.evidence["source_kind"] == "user_declared"
        assert evidence.evidence["externally_verified"] is False
        assert session.scalar(select(func.count()).select_from(AuditEvent)) > 0


def test_local_directory_is_exact_readonly_root(runtime, tmp_path):
    directory = tmp_path / "chosen"
    directory.mkdir()
    code = directory / "main.py"
    code.write_text("user source", encoding="utf-8")
    sibling = tmp_path / "secret.txt"
    sibling.write_text("outside", encoding="utf-8")
    before = code.stat().st_mtime_ns
    result = create_link(runtime, url=None, local_directory=str(directory))
    assert create_link(runtime, url=None, local_directory=str(directory)) == result
    database, settings, registry, _ = runtime
    registry.refresh_if_changed()
    with database.session() as session:
        source = session.scalar(select(Source))
        representation = session.scalar(
            select(Representation).where(Representation.source_id == source.id)
        )
        assert source.read_only
        assert representation.path_ref == "."
        assert representation.tree_digest
        assert source.config["_frozen_manifest_sha256"]
        assert registry.root_for(source.alias, "repository") == directory
        assert registry.resolve(source.alias, "repository", ".") == directory
        with pytest.raises(UnsafeAssetPath):
            registry.resolve(source.alias, "repository", "../secret.txt")
        manifest = Path(source.root_path, source.manifest_path).read_text(encoding="utf-8")
        assert str(directory) not in manifest
    assert code.stat().st_mtime_ns == before
    assert code.read_text(encoding="utf-8") == "user source"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://github.com@evil.test/owner/repo",
        "https://github.com:444/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://gitlab.com/group/../repo",
        "file:///tmp/repo",
        "https://localhost/repo",
    ],
)
def test_download_rejects_uncontrolled_hosts_and_paths(url):
    with pytest.raises(SnapshotError):
        public_repository(url)


def test_other_https_url_can_only_be_linked(runtime):
    result = create_link(
        runtime, url="https://example.org/project?token=secret#fragment", kind="project"
    )
    database, _, _, _ = runtime
    with database.session() as session:
        profile = session.get(RepositoryProfile, UUID(result["repository_asset_id"]))
        assert profile.canonical_url == "https://example.org/project"
        with pytest.raises(SnapshotError, match="linked online"):
            queue_snapshot(session, profile.asset_id, None)


def test_commit_must_be_full_sha_and_no_redirects():
    public = public_repository("https://github.com/owner/repo")
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with httpx.Client(transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(SnapshotError, match="HTTP 302"):
            resolve_commit(client, public, None)
    assert len(requests) == 1
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"sha": "branch-name"})
        )
    ) as client:
        with pytest.raises(SnapshotError, match="40-character"):
            resolve_commit(client, public, None)


@pytest.mark.parametrize(
    "member",
    [
        "repo/../../escape.py",
        "/absolute.py",
        "repo/C:evil",
        "repo\\evil",
        "repo/CON.txt",
        "repo//empty",
        "repo/file.",
    ],
)
def test_archive_path_traversal_fails(tmp_path, member):
    archive = tmp_path / "archive"
    archive.write_bytes(zip_bytes([(member, b"evil")]))
    with pytest.raises(SnapshotError, match="unsafe path"):
        extract_archive(archive, tmp_path / "tree", lambda: None)


def test_zip_symlink_and_tar_hardlink_rejected(tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        member = zipfile.ZipInfo("repo/link")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(member, "../../secret")
    with pytest.raises(SnapshotError, match="symlinks"):
        extract_archive(archive, tmp_path / "tree1", lambda: None)
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tarred:
        member = tarfile.TarInfo("repo/link")
        member.type = tarfile.LNKTYPE
        member.linkname = "../../secret"
        tarred.addfile(member)
    with pytest.raises(SnapshotError, match="hard links"):
        extract_archive(archive, tmp_path / "tree2", lambda: None)


def test_size_and_entry_limits_and_filtered_weights(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.write_bytes(zip_bytes())
    metadata = extract_archive(archive, tmp_path / "tree", lambda: None)
    assert metadata["skipped"]["excluded_weights_datasets_dependencies"] == 1
    assert not (tmp_path / "tree/weights").exists()
    assert (tmp_path / "tree/main.py").exists()
    monkeypatch.setattr(download, "TREE_LIMIT", 2)
    with pytest.raises(SnapshotError, match="extracted tree limit"):
        extract_archive(archive, tmp_path / "limited", lambda: None)
    monkeypatch.setattr(download, "TREE_LIMIT", 500 * 1024 * 1024)
    monkeypatch.setattr(download, "ENTRY_LIMIT", 1)
    with pytest.raises(SnapshotError, match="entry limit"):
        extract_archive(archive, tmp_path / "entries", lambda: None)


def test_worker_snapshot_reuses_sha_and_preserves_old_versions(runtime, monkeypatch):
    result = create_link(runtime, download=True)
    database, settings, registry, _ = runtime
    requests = mocked_client(monkeypatch)
    parameters = {"repository_asset_id": result["repository_asset_id"], "_job_id": result["job_id"]}
    first = run_repository_snapshot(database, settings, registry, parameters)
    assert first["status"] == "downloaded"
    assert first["revision"] == SHA
    path = settings.data_dir / "library/repositories" / result["repository_asset_id"] / SHA
    assert (path / "main.py").read_text(encoding="utf-8") == "print('never executed')"
    second = run_repository_snapshot(database, settings, registry, parameters)
    assert second["status"] == "reused"
    assert second["representation_id"] == first["representation_id"]
    assert len(requests) == 3  # commit lookup on each explicit request, one archive
    mocked_client(monkeypatch, commit=SHA2)
    third = run_repository_snapshot(
        database, settings, registry, {"repository_asset_id": result["repository_asset_id"]}
    )
    assert third["revision"] == SHA2
    assert path.is_dir()
    with database.session() as session:
        representations = list(
            session.scalars(select(Representation).where(Representation.role == "snapshot"))
        )
        assert len(representations) == 2
        assert (
            session.get(RepositoryProfile, UUID(result["repository_asset_id"])).pinned_revision
            == SHA2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.job_type == "extract_representation")
            )
            == 2
        )
        source = session.get(Source, representations[0].source_id)
        assert registry.resolve(source.alias, "repository", SHA) == path


def test_http_failure_and_cancellation_never_register_snapshot(runtime, monkeypatch):
    result = create_link(runtime, download=True)
    database, settings, registry, _ = runtime
    mocked_client(monkeypatch, archive_status=404)
    parameters = {"repository_asset_id": result["repository_asset_id"], "_job_id": result["job_id"]}
    with pytest.raises(SnapshotError, match="HTTP 404"):
        run_repository_snapshot(database, settings, registry, parameters)

    def cancel():
        with database.session() as session:
            session.get(Job, UUID(result["job_id"])).cancel_requested = True

    mocked_client(monkeypatch, on_archive=cancel)
    with pytest.raises(SnapshotCancelled):
        run_repository_snapshot(database, settings, registry, parameters)
    with database.session() as session:
        assert not session.scalar(
            select(Representation.id).where(Representation.role == "snapshot")
        )
        assert not session.scalar(select(Source.id))
    assert not list(
        (settings.data_dir / "library/repositories" / result["repository_asset_id"]).glob(
            ".snapshot-*"
        )
    )


def test_api_contract_idempotency_and_active_job_dedupe(runtime):
    database, settings, registry, paper_id = runtime
    app = create_app(settings, database=database)
    app.state.source_registry = registry
    with TestClient(app) as client:
        body = {
            "paper_id": str(paper_id),
            "url": "https://github.com/example/implementation",
            "kind": "official",
        }
        response = client.post(
            "/api/v1/code-links", json=body, headers={"Idempotency-Key": "link-1"}
        )
        assert response.status_code == 201, response.text
        assert set(response.json()) == {"repository_asset_id", "relation_id"}
        replay = client.post("/api/v1/code-links", json=body, headers={"Idempotency-Key": "link-1"})
        assert replay.json() == response.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        snapshot = {
            "repository_asset_id": response.json()["repository_asset_id"],
            "revision": "main",
        }
        job = client.post("/api/v1/repository-snapshots", json=snapshot)
        assert job.status_code == 202, job.text
        assert set(job.json()) == {"job_id"}
        assert client.post("/api/v1/repository-snapshots", json=snapshot).json() == job.json()
        assert (
            client.post("/api/v1/code-links", json={"paper_id": str(paper_id)}).status_code == 422
        )
        assert (
            client.post(
                "/api/v1/code-links",
                json={**body, "url": "https://other.example/project"},
                headers={"Idempotency-Key": "link-1"},
            ).status_code
            == 409
        )


def test_repository_junction_cannot_redirect_writes_to_frozen_source(
    runtime, tmp_path, monkeypatch
):
    database, settings, registry, _ = runtime
    result = create_link(runtime)
    frozen = tmp_path / "frozen-materials"
    frozen.mkdir()
    sentinel = frozen / "original.py"
    sentinel.write_text("readonly original", encoding="utf-8")
    link = settings.data_dir / "library/repositories"
    link.parent.mkdir(parents=True)
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(frozen)],
            capture_output=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
    else:
        link.symlink_to(frozen, target_is_directory=True)
    try:
        mocked_client(monkeypatch)
        with pytest.raises(ValueError, match="资料目录边界|重解析点|符号链接"):
            run_repository_snapshot(
                database, settings, registry, {"repository_asset_id": result["repository_asset_id"]}
            )
        assert list(frozen.iterdir()) == [sentinel]
        assert sentinel.read_text(encoding="utf-8") == "readonly original"
    finally:
        if os.name == "nt":
            os.rmdir(link)  # remove the test junction itself, never its target
        else:
            link.unlink()


@pytest.mark.parametrize(
    "scenario", ["missing_status", "missing_directory", "changed_source", "changed_tree"]
)
def test_reuse_checks_immutable_source_and_actual_directory(runtime, monkeypatch, scenario):
    result = create_link(runtime)
    database, settings, registry, _ = runtime
    mocked_client(monkeypatch)
    parameters = {"repository_asset_id": result["repository_asset_id"]}
    first = run_repository_snapshot(database, settings, registry, parameters)
    with database.session() as session:
        representation = session.get(Representation, UUID(first["representation_id"]))
        source = session.get(Source, representation.source_id)
        path = registry.resolve(source.alias, "repository", SHA)
        if scenario == "missing_status":
            representation.availability = "missing"
        elif scenario == "missing_directory":
            shutil.rmtree(path)
        elif scenario == "changed_source":
            Path(source.root_path, source.manifest_path).write_text("changed", encoding="utf-8")
        else:
            (path / "main.py").write_text("changed", encoding="utf-8")
    requests = mocked_client(monkeypatch)
    if scenario == "missing_directory":
        restored = run_repository_snapshot(database, settings, registry, parameters)
        assert restored["status"] == "downloaded"
        assert restored["revision"] == SHA
        assert restored["representation_id"] != first["representation_id"]
        with database.session() as session:
            replacement = session.get(Representation, UUID(restored["representation_id"]))
            assert "-restored-" in replacement.path_ref
            assert (
                replacement.attributes["replaces_missing_representation_id"]
                == first["representation_id"]
            )
            assert replacement.source_id != source.id
            assert session.get(Representation, UUID(first["representation_id"])) is not None
        assert len(requests) == 2
    elif scenario == "missing_status":
        reused = run_repository_snapshot(database, settings, registry, parameters)
        assert reused["status"] == "reused"
        assert reused["representation_id"] == first["representation_id"]
        assert len(requests) == 1
        with database.session() as session:
            assert (
                session.get(Representation, UUID(first["representation_id"])).availability
                == "available"
            )
    else:
        with pytest.raises(
            SnapshotError, match="directory has changed|source evidence has changed"
        ):
            run_repository_snapshot(database, settings, registry, parameters)
        assert len(requests) == 1  # changed evidence must never be overwritten
    with database.session() as session:
        assert session.scalar(
            select(func.count())
            .select_from(Representation)
            .where(Representation.role == "snapshot")
        ) == (2 if scenario == "missing_directory" else 1)


def test_retry_keeps_first_resolved_commit_when_branch_moves(runtime, monkeypatch):
    result = create_link(runtime, download=True, revision="main")
    database, settings, registry, _ = runtime
    parameters = {
        "repository_asset_id": result["repository_asset_id"],
        "_job_id": result["job_id"],
        "revision": "main",
    }
    mocked_client(monkeypatch, commit=SHA, archive_status=404)
    with pytest.raises(SnapshotError, match="HTTP 404"):
        run_repository_snapshot(database, settings, registry, parameters)
    with database.session() as session:
        assert session.get(Job, UUID(result["job_id"])).parameters["resolved_revision"] == SHA
    requests = mocked_client(monkeypatch, commit=SHA2)
    finished = run_repository_snapshot(database, settings, registry, parameters)
    assert requests[0].url.path.endswith("/" + SHA)
    assert finished["revision"] == SHA


@pytest.mark.parametrize("decision", ["excluded", "retired"])
def test_snapshot_download_cannot_bypass_audited_serving_decisions(runtime, monkeypatch, decision):
    result = create_link(runtime)
    database, settings, registry, _ = runtime
    mocked_client(monkeypatch)
    parameters = {"repository_asset_id": result["repository_asset_id"]}
    first = run_repository_snapshot(database, settings, registry, parameters)
    with database.session() as session:
        representation = session.get(Representation, UUID(first["representation_id"]))
        representation.availability = "missing"  # restoration must still respect the decision
        if decision == "excluded":
            session.add(
                FieldResolution(
                    entity_type="representation",
                    entity_id=representation.id,
                    field_path="/serving",
                    resolution_kind="override",
                    override_value={"excluded": True},
                    reason="Audited exclusion",
                )
            )
        else:
            backup = BackupRun(status="verified", destination_label="test")
            session.add(backup)
            session.flush()
            session.add(
                SourceRetirement(
                    source_id=representation.source_id,
                    backup_id=backup.id,
                    payload={},
                    payload_digest="a" * 64,
                    frozen_manifest_sha256="b" * 64,
                    frozen_source_config_sha256="c" * 64,
                    record_set_digest="d" * 64,
                    representation_set_digest="e" * 64,
                    mount_set_digest="f" * 64,
                    collection_digest="0" * 64,
                    signature_algorithm="test",
                    signer_key_id="test",
                    signature="test",
                    applied_by="test",
                )
            )
    requests = mocked_client(monkeypatch)
    with pytest.raises(RetiredRepresentationError):
        run_repository_snapshot(database, settings, registry, parameters)
    assert len(requests) == 1
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Representation)
                .where(Representation.role == "snapshot")
            )
            == 1
        )


def test_restoration_retry_reuses_published_path_after_database_rollback(runtime, monkeypatch):
    result = create_link(runtime, download=True)
    database, settings, registry, _ = runtime
    mocked_client(monkeypatch)
    parameters = {"repository_asset_id": result["repository_asset_id"], "_job_id": result["job_id"]}
    first = run_repository_snapshot(database, settings, registry, parameters)
    base = settings.data_dir / "library/repositories" / result["repository_asset_id"]
    shutil.rmtree(base / SHA)
    with database.session() as session:
        session.get(Representation, UUID(first["representation_id"])).availability = "missing"
    audit = snapshot_service._audit

    def fail_after_source_registration(session, entity_type, entity_id, after, request_id=None):
        if entity_type == "representation":
            raise RuntimeError("simulated transaction rollback")
        audit(session, entity_type, entity_id, after, request_id)

    monkeypatch.setattr(snapshot_service, "_audit", fail_after_source_registration)
    with pytest.raises(RuntimeError, match="transaction rollback"):
        run_repository_snapshot(database, settings, registry, parameters)
    with database.session() as session:
        path_ref = session.get(Job, UUID(result["job_id"])).parameters["recovery_path_ref"]
        assert session.scalar(select(func.count()).select_from(Source)) == 1
    assert (base / path_ref).is_dir()
    assert len([entry for entry in base.iterdir() if entry.is_dir()]) == 1
    monkeypatch.setattr(snapshot_service, "_audit", audit)
    restored = run_repository_snapshot(database, settings, registry, parameters)
    assert restored["status"] == "downloaded"
    with database.session() as session:
        representation = session.get(Representation, UUID(restored["representation_id"]))
        assert representation.path_ref == path_ref
        assert session.scalar(select(func.count()).select_from(Source)) == 2
        assert session.get(Job, UUID(result["job_id"])).parameters["recovery_path_ref"] == path_ref
    assert len([entry for entry in base.iterdir() if entry.is_dir()]) == 1


@pytest.mark.parametrize("mode", ["local_directory", "download", "restored"])
def test_native_source_integrity_accepts_snapshot_then_detects_tampering(
    runtime, tmp_path, monkeypatch, mode
):
    database, settings, registry, _ = runtime
    if mode == "local_directory":
        directory = tmp_path / "chosen-native"
        directory.mkdir()
        (directory / "main.py").write_text("original", encoding="utf-8")
        result = create_link(runtime, url=None, local_directory=str(directory))
        registry.refresh_if_changed()
        with database.session() as session:
            representation = session.scalar(
                select(Representation).where(
                    Representation.asset_id == UUID(result["repository_asset_id"])
                )
            )
            representation_id = representation.id
        target = directory / "main.py"
    else:
        result = create_link(runtime, download=True)
        mocked_client(monkeypatch)
        parameters = {
            "repository_asset_id": result["repository_asset_id"],
            "_job_id": result["job_id"],
        }
        snapshot = run_repository_snapshot(database, settings, registry, parameters)
        if mode == "restored":
            original = (
                settings.data_dir / "library/repositories" / result["repository_asset_id"] / SHA
            )
            shutil.rmtree(original)
            snapshot = run_repository_snapshot(database, settings, registry, parameters)
            with database.session() as session:
                job = session.get(Job, UUID(result["job_id"]))
                assert job.parameters["resolved_revision"] == SHA
                path_ref = job.parameters["recovery_path_ref"]
                restored = session.get(Representation, UUID(snapshot["representation_id"]))
                assert restored.path_ref == path_ref
                assert restored.revision == SHA
        representation_id = UUID(snapshot["representation_id"])
        with database.session() as session:
            representation = session.get(Representation, representation_id)
            source = session.get(Source, representation.source_id)
            target = (
                registry.resolve(source.alias, "repository", representation.path_ref) / "main.py"
            )
    # This is the actual validator invoked by scripts/verify_source_integrity.py.
    with database.session() as session:
        clean = verify_source_integrity(session, registry).to_dict()
        assert clean["status"] == "passed", clean
        assert clean["counts"]["directories_verified"] == 1
        if mode == "restored":
            assert clean["counts"]["missing_confirmed"] == 1
    target.write_text("modified content with a different source size", encoding="utf-8")
    with database.session() as session:
        tampered = verify_source_integrity(session, registry).to_dict()
        assert tampered["status"] == "failed"
        assert any(
            "tree_digest_mismatch" in mismatch["reasons"] for mismatch in tampered["mismatches"]
        ), tampered
