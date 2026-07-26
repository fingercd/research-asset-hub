from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.web_host import create_app


def test_default_local_port_contract() -> None:
    settings = ApiSettings()

    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8765
    assert settings.asset_gateway_host == "127.0.0.1"
    assert settings.asset_gateway_port == 8766
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8767
    assert settings.web_origin == "http://127.0.0.1:8765"
    assert settings.gateway_origin == "http://127.0.0.1:8766"


def test_production_web_host_serves_dist_and_spa_fallback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><main>Hub</main>", encoding="utf-8")
    (assets / "app.js").write_text("globalThis.HUB = true;", encoding="utf-8")
    app = create_app(
        ApiSettings(
            web_dist_path=dist,
            api_port=65534,
        )
    )
    with TestClient(app) as client:
        root = client.get("/")
        nested = client.get("/domains/agent")
        javascript = client.get("/assets/app.js")
        rejected_host = client.get("/", headers={"Host": "public.example"})
    assert root.status_code == 200
    assert "<main>Hub</main>" in root.text
    assert nested.status_code == 200
    assert "<main>Hub</main>" in nested.text
    assert javascript.status_code == 200
    assert "globalThis.HUB" in javascript.text
    assert rejected_host.status_code == 400


def test_production_web_health_reports_static_and_api_readiness(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = create_app(
        ApiSettings(
            web_dist_path=dist,
            api_port=65534,
            web_api_proxy_timeout_seconds=1,
        )
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "research-asset-hub-web"
    assert payload["status"] == "degraded"
    assert payload["static_frontend"] == "ready"
    assert payload["api"] == "unavailable"
    assert "local API health is unavailable" in payload["detail"]


def test_production_web_host_reports_api_unavailable_as_problem(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = create_app(ApiSettings(web_dist_path=dist, api_port=65534))
    with TestClient(app) as client:
        response = client.get("/api/v1/assets")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")


def test_production_web_host_uses_configurable_api_proxy_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    captured: dict[str, object] = {}

    class RecordingAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def request(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"items": []})

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("apps.api.web_host.httpx.AsyncClient", RecordingAsyncClient)
    app = create_app(
        ApiSettings(
            web_dist_path=dist,
            web_api_proxy_timeout_seconds=240,
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/assets")

    assert response.status_code == 200
    assert captured["timeout"] == 240


def test_api_proxy_timeout_supports_hub_environment_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_WEB_API_PROXY_TIMEOUT_SECONDS", "300")

    assert ApiSettings().web_api_proxy_timeout_seconds == 300
