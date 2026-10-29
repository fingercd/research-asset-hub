from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    ExternalIdentifierModel,
    Job,
    Representation,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRetirement,
)
from scripts.seed_jobs import main, seed_jobs


@pytest.fixture
def database() -> Iterator[DatabaseManager]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    yield DatabaseManager(settings, engine=engine, require_migrations=False)
    engine.dispose()


def test_source_filter_only_seeds_target_html_and_reuses_jobs(
    database: DatabaseManager,
) -> None:
    with database.session() as session:
        target = Source(
            alias="target-guides", label="Target", adapter="generic", root_path="/target"
        )
        other = Source(alias="other-guides", label="Other", adapter="generic", root_path="/other")
        asset = Asset(asset_type="paper", title="Shared paper")
        session.add_all([target, other, asset])
        session.flush()
        session.add(SourceRecord(source_id=target.id, source_record_id="paper", asset_id=asset.id))
        representations = [
            Representation(
                asset_id=asset.id,
                source_id=source.id,
                representation_type=kind,
                role="guide",
                materialization_kind="file",
                availability=availability,
                path_ref=path,
                sha256="a" * 64,
                deleted_at=deleted_at,
            )
            for source, kind, path, availability, deleted_at in (
                (target, "html", "first.html", "available", None),
                (target, "html", "second.html", "available", None),
                (other, "html", "other.html", "available", None),
                (target, "pdf", "paper.pdf", "available", None),
                (target, "html", "missing.html", "missing", None),
                (target, "html", "deleted.html", "available", datetime.now(UTC)),
            )
        ]
        session.add_all(representations)
        session.flush()
        expected_ids = {str(item.id) for item in representations[:2]}

    first = seed_jobs(
        database,
        include_assets=False,
        include_representations=True,
        max_representations=None,
        representation_types=frozenset({"html"}),
        source_alias="target-guides",
    )
    second = seed_jobs(
        database,
        include_assets=False,
        include_representations=True,
        max_representations=None,
        representation_types=frozenset({"html"}),
        source_alias="target-guides",
    )

    assert first == {"representation_jobs_created": 2}
    assert second == {"representation_jobs_reused": 2}
    with database.session() as session:
        jobs = list(session.scalars(select(Job)).all())
    assert len(jobs) == 2
    assert {job.job_type for job in jobs} == {"extract_representation"}
    assert {job.parameters["representation_id"] for job in jobs} == expected_ids


def test_unknown_source_fails_without_seeding(database: DatabaseManager) -> None:
    with pytest.raises(ValueError, match="Unknown source alias"):
        seed_jobs(
            database,
            include_assets=False,
            include_representations=True,
            max_representations=None,
            source_alias="unknown-source",
        )
    with database.session() as session:
        assert session.scalar(select(Job.id)) is None


@pytest.mark.parametrize("only", ["assets", "all"])
def test_cli_supports_source_for_asset_seeding(
    database: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    only: str,
) -> None:
    with database.session() as session:
        source = Source(alias="target", label="Target", adapter="generic", root_path="/target")
        paper = Asset(asset_type="paper", title="Target paper")
        repository = Asset(asset_type="repository", title="Target repository")
        other = Asset(asset_type="paper", title="Other paper")
        session.add_all([source, paper, repository, other])
        session.flush()
        record = SourceRecord(source_id=source.id, source_record_id="paper", asset_id=paper.id)
        session.add(record)
        session.flush()
        session.add(
            SourceRecordAsset(source_record_id=record.id, asset_id=repository.id, role="repository")
        )
        expected_ids = {str(paper.id), str(repository.id)}
    monkeypatch.setattr("scripts.seed_jobs.DatabaseManager", lambda settings: database)
    monkeypatch.setattr("sys.argv", ["seed_jobs.py", "--source", "target", "--only", only])
    assert main() == 0
    assert "asset_jobs_created=2" in capsys.readouterr().out
    with database.session() as session:
        jobs = session.scalars(select(Job)).all()
    assert {job.parameters["asset_id"] for job in jobs} == expected_ids


def test_cli_reports_unknown_source(
    database: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("scripts.seed_jobs.DatabaseManager", lambda settings: database)
    monkeypatch.setattr(
        "sys.argv", ["seed_jobs.py", "--source", "unknown", "--only", "representations"]
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert "Unknown source alias: 'unknown'" in capsys.readouterr().err


def test_asset_uuid_scope_seeds_only_active_available_local_representations(
    database: DatabaseManager,
) -> None:
    with database.session() as session:
        active = Source(alias="active", label="Active", adapter="generic", root_path="/active")
        retired = Source(alias="retired", label="Retired", adapter="generic", root_path="/retired")
        asset = Asset(asset_type="paper", title="Scoped")
        other = Asset(asset_type="paper", title="Other")
        session.add_all([active, retired, asset, other])
        session.flush()
        representations = [
            Representation(
                asset_id=target.id,
                source_id=source.id,
                representation_type="markdown",
                role="original",
                materialization_kind=kind,
                availability=availability,
                path_ref=path,
                external_url="https://example.com/paper" if kind == "url" else None,
            )
            for target, source, kind, availability, path in (
                (asset, active, "file", "available", "active.md"),
                (asset, retired, "file", "available", "retired.md"),
                (asset, active, "url", "available", None),
                (asset, active, "file", "missing", "missing.md"),
                (other, active, "file", "available", "other.md"),
            )
        ]
        session.add_all(representations)
        backup = BackupRun(destination_label="test")
        session.add(backup)
        session.flush()
        session.add(
            SourceRetirement(
                source_id=retired.id,
                backup_id=backup.id,
                payload={},
                payload_digest="a" * 64,
                frozen_manifest_sha256="a" * 64,
                frozen_source_config_sha256="a" * 64,
                record_set_digest="a" * 64,
                representation_set_digest="a" * 64,
                mount_set_digest="a" * 64,
                collection_digest="a" * 64,
                signature_algorithm="hmac-sha256",
                signer_key_id="test",
                signature="test",
                applied_by="pytest",
            )
        )
        asset_id = asset.id
        expected_representation_id = str(representations[0].id)
    first = seed_jobs(
        database,
        include_assets=True,
        include_representations=True,
        max_representations=None,
        asset_ids=(asset_id,),
    )
    second = seed_jobs(
        database,
        include_assets=True,
        include_representations=True,
        max_representations=None,
        asset_ids=(asset_id,),
    )
    assert first == {"asset_jobs_created": 1, "representation_jobs_created": 1}
    assert second == {"asset_jobs_reused": 1, "representation_jobs_reused": 1}
    with database.session() as session:
        jobs = session.scalars(select(Job)).all()
    assert len(jobs) == 2
    assert next(job for job in jobs if job.job_type == "index_asset").parameters == {
        "asset_id": str(asset_id),
    }
    assert next(job for job in jobs if job.job_type == "extract_representation").parameters == {
        "representation_id": expected_representation_id,
    }


def test_failed_existing_jobs_are_visible_and_cli_returns_failure(
    database: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with database.session() as session:
        asset = Asset(asset_type="paper", title="Failed indexing")
        session.add(asset)
        session.flush()
        asset_id = asset.id
    seed_jobs(
        database,
        include_assets=True,
        include_representations=False,
        max_representations=None,
        asset_ids=(asset_id,),
    )
    with database.session() as session:
        job = session.scalar(select(Job))
        assert job is not None
        job.status = "failed"
    second = seed_jobs(
        database,
        include_assets=True,
        include_representations=False,
        max_representations=None,
        asset_ids=(asset_id,),
    )
    assert second == {"asset_jobs_failed": 1}
    monkeypatch.setattr("scripts.seed_jobs.DatabaseManager", lambda settings: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "seed_jobs.py",
            "--only",
            "assets",
            "--asset-id",
            str(asset_id),
        ],
    )
    assert main() == 1
    assert "asset_jobs_failed=1" in capsys.readouterr().out
    with database.session() as session:
        assert len(session.scalars(select(Job)).all()) == 1


def test_asset_fingerprint_reuses_identical_input_and_indexes_changed_identity(
    database: DatabaseManager,
) -> None:
    with database.session() as session:
        asset = Asset(asset_type="paper", title="Original title")
        session.add(asset)
        session.flush()
        asset_id = asset.id
    assert seed_jobs(
        database,
        include_assets=True,
        include_representations=False,
        max_representations=None,
        asset_ids=(asset_id,),
    ) == {"asset_jobs_created": 1}
    with database.session() as session:
        session.add(
            ExternalIdentifierModel(
                asset_id=asset_id,
                scheme="doi",
                value="10.1000/new",
                canonical="10.1000/new",
            )
        )
    assert seed_jobs(
        database,
        include_assets=True,
        include_representations=False,
        max_representations=None,
        asset_ids=(asset_id,),
    ) == {"asset_jobs_created": 1}
    assert seed_jobs(
        database,
        include_assets=True,
        include_representations=False,
        max_representations=None,
        asset_ids=(asset_id,),
    ) == {"asset_jobs_reused": 1}
    with database.session() as session:
        jobs = list(session.scalars(select(Job)).all())
    assert len(jobs) == 2
    assert len({job.dedupe_key for job in jobs}) == 2
