"""桌面壳 Origin 白名单:CORS 与浏览器写保护必须放行 Tauri origin。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.api.main import create_app


def _client() -> TestClient:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    app = create_app(settings=settings, database=DatabaseManager(settings))
    return TestClient(app)


def test_cors_preflight_allows_desktop_origin() -> None:
    client = _client()
    response = client.options(
        "/api/v1/domains",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert "http://tauri.localhost" in response.headers["access-control-allow-origin"]


def test_cors_preflight_allows_csrf_header_for_desktop_origin() -> None:
    """写请求预检:ACRH 携带 content-type 与 x-research-hub-csrf 必须放行。"""
    client = _client()
    response = client.options(
        "/api/v1/annotations",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-research-hub-csrf",
        },
    )
    assert response.status_code == 200
    assert "http://tauri.localhost" in response.headers["access-control-allow-origin"]
    assert (
        "x-research-hub-csrf"
        in response.headers["access-control-allow-headers"].lower()
    )


def test_write_guard_accepts_desktop_origin_with_csrf() -> None:
    client = _client()
    response = client.post(
        "/api/v1/annotations",
        json={"asset_id": "00000000-0000-0000-0000-000000000000", "content": "x"},
        headers={
            "Origin": "tauri://localhost",
            "X-Research-Hub-CSRF": "1",
        },
    )
    # 业务校验会在 404/422 之后,这里只关心它没有被 CSRF 中间件拦截为 403 csrf_rejected
    assert response.status_code != 403


def test_write_guard_still_rejects_unknown_origin() -> None:
    client = _client()
    response = client.post(
        "/api/v1/annotations",
        json={"asset_id": "00000000-0000-0000-0000-000000000000", "content": "x"},
        headers={"Origin": "http://evil.example.com", "X-Research-Hub-CSRF": "1"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"


def test_desktop_origins_env_override() -> None:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        desktop_origins=["http://custom.localhost"],
    )
    assert settings.desktop_origins == ["http://custom.localhost"]
