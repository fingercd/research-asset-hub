from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from packages.domain.models import Asset, Base, Job, Representation, Source
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
def test_cli_rejects_source_for_asset_seeding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    only: str,
) -> None:
    monkeypatch.setattr("sys.argv", ["seed_jobs.py", "--source", "target", "--only", only])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert "--source requires --only representations" in capsys.readouterr().err


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
