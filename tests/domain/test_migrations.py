from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from packages.domain.identifiers import new_uuid7
from packages.domain.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "migrations" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_0002_backfills_legacy_source_mapping_and_0003_matches_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = (tmp_path / "migration.sqlite").resolve()
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "0001_initial_domain")
    engine = sa.create_engine(database_url)
    source_id = new_uuid7()
    asset_id = new_uuid7()
    source_record_id = new_uuid7()
    identifier_id = new_uuid7()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO sources "
                "(id, alias, label, adapter, root_path, read_only, enabled, config) "
                "VALUES (:id, 'fixture', 'Fixture', 'generic', '.', 1, 1, :config)"
            ),
            {"id": source_id.hex, "config": "{}"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO assets "
                "(id, asset_type, title, authors, analysis, visibility, source_urls, "
                "domain_slugs, tags, attributes) "
                "VALUES (:id, 'paper', 'Legacy paper', :authors, :analysis, 'private', "
                ":urls, :domains, :tags, :attributes)"
            ),
            {
                "id": asset_id.hex,
                "authors": "[]",
                "analysis": "{}",
                "urls": "[]",
                "domains": "[]",
                "tags": "[]",
                "attributes": "{}",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO source_records "
                "(id, source_id, source_record_id, asset_id, outcome, raw_data, "
                "source_fields, warnings) "
                "VALUES (:id, :source_id, 'legacy-1', :asset_id, 'ready', :raw, "
                ":fields, :warnings)"
            ),
            {
                "id": source_record_id.hex,
                "source_id": source_id.hex,
                "asset_id": asset_id.hex,
                "raw": '{"title":"Legacy paper"}',
                "fields": '{"title":"title"}',
                "warnings": "[]",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO external_identifiers "
                "(id, asset_id, scheme, value, canonical) "
                "VALUES (:id, :asset_id, 'url', 'https://example.org/p', "
                "'https://example.org/p')"
            ),
            {"id": identifier_id.hex, "asset_id": asset_id.hex},
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert set(Base.metadata.tables) <= tables
        mapping = connection.execute(
            sa.text(
                "SELECT asset_id, role, source_record_version_id "
                "FROM source_record_assets WHERE source_record_id = :record_id"
            ),
            {"record_id": source_record_id.hex},
        ).mappings().one()
        assert mapping["asset_id"] == asset_id.hex
        assert mapping["role"] == "primary"
        assert mapping["source_record_version_id"] == source_record_id.hex

        version_count = connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM source_record_versions "
                "WHERE source_record_id = :record_id"
            ),
            {"record_id": source_record_id.hex},
        )
        assert version_count == 1
        identifier = connection.execute(
            sa.text(
                "SELECT scope, is_identity FROM external_identifiers WHERE id = :id"
            ),
            {"id": identifier_id.hex},
        ).mappings().one()
        assert identifier["scope"] == ""
        assert not identifier["is_identity"]

    command.check(config)
    command.downgrade(config, "0001_initial_domain")
    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert "source_record_assets" not in tables
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM source_records")) == 1
    engine.dispose()


def test_required_revision_default_matches_migration_head() -> None:
    """新增迁移时若忘记更新默认值，本测试直接失败。"""
    script_dir = ScriptDirectory.from_config(_config("sqlite://"))
    head = script_dir.get_current_head()
    default = ApiSettings.model_fields["database_required_revision"].default
    assert default == head


def test_readiness_accepts_revision_at_or_after_required(tmp_path: Path) -> None:
    """数据库修订超前于 required 时应 ready；落后时应 unmigrated。"""
    database_path = (tmp_path / "readiness.sqlite").resolve()
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "head")
    engine = sa.create_engine(database_url)
    try:
        ahead = DatabaseManager(
            ApiSettings(
                database_url=database_url,
                database_required_revision="0002_platform_core",
            ),
            engine=engine,
        )
        assert ahead.check(force=True).status == "ready"

        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = '0002_platform_core'")
            )
        behind = DatabaseManager(
            ApiSettings(
                database_url=database_url,
                database_required_revision="0003_search_rag_integrations",
            ),
            engine=engine,
        )
        readiness = behind.check(force=True)
        assert readiness.status == "unmigrated"
        assert "expected" in readiness.detail
    finally:
        engine.dispose()
