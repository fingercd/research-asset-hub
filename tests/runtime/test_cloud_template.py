"""Regression gates for retiring standalone website deployment, not cloud acceptance."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_no_executable_web_or_compose_deployment_templates_remain() -> None:
    deploy = PROJECT_ROOT / "deploy"
    assert not (deploy / "compose.yaml").exists()
    assert not (deploy / "cloud" / "compose.cloud.yaml").exists()
    assert not (deploy / "cloud" / "Dockerfile.cloud").exists()
    assert not (deploy / "cloud" / "bin" / "run_hub.py").exists()
    assert not any(path.is_file() for path in (deploy / "cloud").rglob("*"))


def test_package_scripts_only_build_static_frontend_and_run_native_desktop() -> None:
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    web = json.loads((PROJECT_ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
    assert "dev:web" not in package["scripts"]
    assert "desktop.ps1" in package["scripts"]["desktop"]
    assert "dev" not in web["scripts"]
    assert "test:e2e" not in web["scripts"]
    assert "vite build" in web["scripts"]["build"]
    assert not (PROJECT_ROOT / "apps/web/playwright.config.ts").exists()
    assert not list((PROJECT_ROOT / "apps/web/e2e").glob("*.spec.ts"))


def test_vite_has_no_independent_website_server_configuration() -> None:
    vite = (PROJECT_ROOT / "apps/web/vite.config.ts").read_text(encoding="utf-8")
    for retired in ("server:", "preview:", "proxy:", "8765", "4173", "1430", "HUB_API_ORIGIN"):
        assert retired not in vite
