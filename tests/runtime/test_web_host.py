from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.web_host import create_app


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


def test_production_web_host_reports_api_unavailable_as_problem(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = create_app(ApiSettings(web_dist_path=dist, api_port=65534))
    with TestClient(app) as client:
        response = client.get("/api/v1/assets")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
