from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Relation,
    Representation,
    Source,
    SourceRetirement,
)


@pytest.fixture
def comparison_api(tmp_path: Path) -> Iterator[tuple[TestClient, DatabaseManager, Engine]]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:", database_require_migrations=False,
        gateway_secret="c" * 64, sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
    )
    settings.sources_config_path.write_text(
        "schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8",
    )
    settings.taxonomy_config_path.write_text("schema_version: 1\ndomains: []\n", encoding="utf-8")
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with TestClient(create_app(settings, database=database)) as client:
        yield client, database, engine
    engine.dispose()


def _seed(database: DatabaseManager, state: str) -> tuple[list[str], str]:
    with database.session() as session:
        paper = Asset(title="Arxiv-only paper", asset_type="paper", source_urls=["https://arxiv.org/abs/1234.5678"])
        second = Asset(title="Other paper", asset_type="paper")
        repository = Asset(title="Confirmed repository", asset_type="repository")
        source = Source(
            alias="comparison", label="Comparison", adapter="generic_manifest", root_path=".",
        )
        session.add_all([paper, second, repository, source])
        session.flush()
        session.add(Relation(subject_asset_id=paper.id, object_asset_id=repository.id,
                             relation_type="implemented_by", confirmation_status="confirmed"))
        representation = Representation(
            asset_id=repository.id, source_id=source.id, representation_type="repository",
            role="primary", materialization_kind="url" if state == "remote" else "directory",
            availability="missing" if state == "missing" else "available",
            path_ref=None if state == "remote" else "repo",
            external_url="https://github.com/example/repo", network_required=state == "remote",
        )
        session.add(representation)
        session.flush()
        if state == "retired":
            backup = BackupRun(destination_label="comparison-test")
            session.add(backup)
            session.flush()
            digest = "a" * 64
            session.add(SourceRetirement(
                source_id=source.id, backup_id=backup.id, payload={}, payload_digest=digest,
                frozen_manifest_sha256=digest, frozen_source_config_sha256=digest,
                record_set_digest=digest, representation_set_digest=digest,
                mount_set_digest=digest, collection_digest=digest,
                signature_algorithm="hmac-sha256", signer_key_id="test",
                signature="test", applied_by="pytest",
            ))
        return [str(paper.id), str(second.id)], str(representation.id)


@pytest.mark.parametrize("state, expected", [
    ("local", "available"), ("remote", "network_required"),
    ("missing", "missing"), ("retired", "not_provided"),
])
def test_saved_and_new_comparisons_use_serving_code_actions(
    comparison_api: tuple[TestClient, DatabaseManager, Engine], state: str, expected: str,
) -> None:
    client, database, _ = comparison_api
    ids, representation_id = _seed(database, state)
    response = client.post("/api/v1/comparisons", json={"asset_ids": ids})
    assert response.status_code == 201, response.text
    new_asset = response.json()["assets"][0]
    saved_asset = client.get("/api/v1/comparisons").json()["items"][0]["assets"][0]
    detail = client.get(f"/api/v1/assets/{ids[0]}").json()
    for asset in (new_asset, saved_asset):
        code = next(action for action in asset["quick_actions"] if action["kind"] == "code")
        detail_code = next(action for action in detail["quick_actions"] if action["kind"] == "code")
        assert code == detail_code
        assert code["status"] == expected
        if state != "retired":
            assert code["representation_id"] == representation_id
            assert asset["related_repositories"][0]["representations"][0]["id"] == representation_id
        else:
            assert not asset["related_repositories"][0]["representations"]
        assert asset["representations"] == []


def test_comparison_list_batches_assets_across_saved_views(
    comparison_api: tuple[TestClient, DatabaseManager, Engine],
) -> None:
    client, database, engine = comparison_api
    ids, _ = _seed(database, "local")
    client.post("/api/v1/comparisons", json={"asset_ids": ids, "title": "One"})
    statements: list[str] = []

    def track(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", track)
    try:
        assert client.get("/api/v1/comparisons").status_code == 200
        first_count = len(statements)
        for index in range(5):
            client.post("/api/v1/comparisons", json={"asset_ids": ids, "title": f"View {index}"})
        statements.clear()
        response = client.get("/api/v1/comparisons")
        assert response.status_code == 200
        assert response.json()["total"] == 6
        assert len(statements) == first_count
    finally:
        event.remove(engine, "before_cursor_execute", track)
