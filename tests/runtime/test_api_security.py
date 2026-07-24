from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import Base


@pytest.fixture
def secured_api() -> Iterator[TestClient]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    app = create_app(settings, database=database)

    @app.get("/api/test/boom")
    def boom() -> None:
        raise ValueError("private payload must not be returned")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    engine.dispose()


def test_browser_write_requires_allowed_origin_and_csrf_header(
    secured_api: TestClient,
) -> None:
    foreign = secured_api.post(
        "/api/v1/assets",
        headers={"Origin": "https://malicious.example"},
        json={},
    )
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "csrf_rejected"

    missing_token = secured_api.post(
        "/api/v1/assets",
        headers={"Origin": "http://127.0.0.1:8765"},
        json={},
    )
    assert missing_token.status_code == 403

    allowed = secured_api.post(
        "/api/v1/assets",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-Research-Hub-CSRF": "1",
        },
        json={},
    )
    assert allowed.status_code == 422
    assert allowed.headers["content-type"].startswith("application/problem+json")
    assert allowed.json()["code"] == "validation_error"


def test_unexpected_errors_are_problem_json_without_private_detail(
    secured_api: TestClient,
) -> None:
    response = secured_api.get("/api/test/boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "internal_error"
    assert "private payload" not in response.text
