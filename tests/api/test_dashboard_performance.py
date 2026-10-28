"""Dashboard keeps its counts while bounding SQL work by request, not domain count."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import Annotation, Asset, Base, IndexState, ReadingState


def test_dashboard_reuses_asset_context_and_counts_only_visible_indexed_assets(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        "schema_version: 1\ndomains:\n"
        "  - id: alpha\n    label: Alpha\n"
        "  - id: beta\n    label: Beta\n"
        "  - id: gamma\n    label: Gamma\n",
        encoding="utf-8",
    )
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        sources_config_path=sources,
        taxonomy_config_path=taxonomy,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)

    with database.session_factory() as session:
        first = Asset(
            asset_type="paper",
            title="First",
            domain_slugs=["alpha", "beta"],
            content="large original text " * 10_000,
            attributes={"code_availability": {"audit": "large evidence " * 10_000}},
        )
        second = Asset(asset_type="paper", title="Second", domain_slugs=["alpha", "gamma"])
        hidden = Asset(
            asset_type="collection",
            title="Hidden portal",
            domain_slugs=["alpha"],
            attributes={"system_hidden": True},
        )
        session.add_all([first, second, hidden])
        session.flush()
        session.add(ReadingState(asset_id=first.id, user_id="local", status="reading"))
        session.add_all(
            [
                Annotation(asset_id=first.id, summary="First note"),
                Annotation(asset_id=first.id, summary="Second note"),
                Annotation(asset_id=second.id, summary="Third note"),
            ]
        )
        for asset_id, digest in [
            (first.id, "a" * 64),
            (first.id, "b" * 64),
            (hidden.id, "c" * 64),
        ]:
            session.add(
                IndexState(
                    index_name="documents",
                    index_kind="keyword",
                    entity_type="asset",
                    entity_id=asset_id,
                    content_digest=digest,
                    version="1",
                    status="ready",
                )
            )
        session.commit()

    database.check()
    statements: list[str] = []

    def record_sql(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with TestClient(create_app(settings, database=database)) as client:
            response = client.get("/api/v1/dashboard")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stats"]["assets"] == 2
        assert body["stats"]["reading"] == 1
        assert body["stats"]["annotations"] == 3
        assert body["index_state"]["indexed_assets"] == 1
        assert body["index_state"]["pending_assets"] == 1
        assert len(response.content) < 100_000
        assert "content" not in body["recent_assets"][0]
        assert all("code_availability" not in item["attributes"] for item in body["recent_assets"])

        domains = {domain["slug"]: domain for domain in body["domains"]}
        assert domains["alpha"]["asset_count"] == 2
        assert domains["alpha"]["annotation_count"] == 3
        assert domains["alpha"]["reading_counts"]["reading"] == 1
        assert all("recent_assets" not in domain for domain in domains.values())
        assert domains["beta"]["annotation_count"] == 2
        assert domains["gamma"]["annotation_count"] == 1
        assert "Hidden portal" not in str(body)

        annotation_queries = [sql for sql in statements if "from annotations" in sql]
        # Only the home card context, grouped domain counts and global count:
        # dashboard does not build an unused per-domain recent card context.
        assert len(annotation_queries) == 3
        index_queries = [sql for sql in statements if "from index_states" in sql]
        assert len(index_queries) == 1
        assert "exists" in index_queries[0]
        assert "index_name" in index_queries[0]
        assert "distinct" not in index_queries[0]
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
        engine.dispose()


def test_dashboard_omits_domain_recent_work_and_preserves_all_domain_counts(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        "domains:\n"
        + "".join(f"  - id: domain-{index}\n    label: Domain {index}\n" for index in range(16)),
        encoding="utf-8",
    )
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        sources_config_path=sources,
        taxonomy_config_path=taxonomy,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with database.session() as session:
        session.add_all(
            Asset(
                asset_type="paper",
                title=f"Paper {index}",
                domain_slugs=[f"domain-{index // 5}"],
                abstract="abstract " * 300,
                summary="summary " * 300,
            )
            for index in range(80)
        )

    statements: list[tuple[str, object]] = []

    def record_sql(
        _connection: object, _cursor: object, sql: str, params: object, *_args: object
    ) -> None:
        statements.append((sql.lower(), params))

    with TestClient(create_app(settings, database=database)) as client:
        event.listen(engine, "before_cursor_execute", record_sql)
        try:
            response = client.get("/api/v1/dashboard")
        finally:
            event.remove(engine, "before_cursor_execute", record_sql)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stats"]["assets"] == 80
        assert len(body["recent_assets"]) == 8
        assert len(response.content) < 60_000
        assert len(json.dumps(body["domains"])) < 10_000
        assert all("recent_assets" not in domain for domain in body["domains"])
        assert all("row_number()" not in sql for sql, _ in statements)
        assert all("json_group_array" not in sql for sql, _ in statements)
        asset_loads = [
            (sql, params)
            for sql, params in statements
            if "assets.summary" in sql and "where assets.id in" in sql
        ]
        assert len(asset_loads) == 1
        assert "where assets.id in" in asset_loads[0][0]
        assert sum(isinstance(value, str) and len(value) == 32 for value in asset_loads[0][1]) == 8

        # Other consumers retain the five-card domain feature by default, and
        # every count/status field remains identical to the dashboard response.
        ordinary = client.get("/api/v1/domains")
        assert ordinary.status_code == 200, ordinary.text
        domains = ordinary.json()["items"]
        assert len(domains) == 16
        assert all(len(domain["recent_assets"]) == 5 for domain in domains)
        assert len(ordinary.content) > 400_000
        assert body["domains"] == [
            {key: value for key, value in domain.items() if key != "recent_assets"}
            for domain in domains
        ]
    engine.dispose()
