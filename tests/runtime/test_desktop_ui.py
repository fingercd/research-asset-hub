"""Desktop UI workspace and static build boundary regression gates."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_PACKAGE = "@research-asset-hub/desktop-ui"


def test_no_executable_web_or_compose_deployment_templates_remain() -> None:
    deploy = PROJECT_ROOT / "deploy"
    assert not (deploy / "compose.yaml").exists()
    assert not (deploy / "cloud" / "compose.cloud.yaml").exists()
    assert not (deploy / "cloud" / "Dockerfile.cloud").exists()
    assert not (deploy / "cloud" / "bin" / "run_hub.py").exists()
    assert not any(path.is_file() for path in (deploy / "cloud").rglob("*"))


def test_package_scripts_only_build_static_frontend_and_run_native_desktop() -> None:
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    ui = json.loads((PROJECT_ROOT / "desktop/ui/package.json").read_text(encoding="utf-8"))
    assert ui["name"] == UI_PACKAGE
    for command in ("build", "lint", "typecheck", "test"):
        assert package["scripts"][command] == f"pnpm --filter {UI_PACKAGE} {command}"
    assert "dev:web" not in package["scripts"]
    assert "desktop.ps1" in package["scripts"]["desktop"]
    assert "dev" not in ui["scripts"]
    assert "test:e2e" not in ui["scripts"]
    assert "vite build" in ui["scripts"]["build"]
    assert not (PROJECT_ROOT / "desktop/ui/playwright.config.ts").exists()
    assert not list((PROJECT_ROOT / "desktop/ui/e2e").glob("*.spec.ts"))


def test_vite_has_no_independent_website_server_configuration() -> None:
    vite = (PROJECT_ROOT / "desktop/ui/vite.config.ts").read_text(encoding="utf-8")
    for retired in ("server:", "preview:", "proxy:", "8765", "4173", "1430", "HUB_API_ORIGIN"):
        assert retired not in vite


def test_workspace_and_tauri_embed_the_desktop_ui_directory() -> None:
    workspace = (PROJECT_ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    lock = (PROJECT_ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    assert '"desktop/ui"' in workspace
    assert "  desktop/ui:" in lock
    assert not (PROJECT_ROOT / "apps" / "web").exists()
    tauri_root = PROJECT_ROOT / "desktop/src-tauri"
    config = json.loads((tauri_root / "tauri.conf.json").read_text(encoding="utf-8"))
    assert config["build"]["beforeBuildCommand"] == f"pnpm --filter {UI_PACKAGE} build"
    assert (tauri_root / config["build"]["frontendDist"]).resolve() == (
        PROJECT_ROOT / "desktop/ui/dist"
    ).resolve()


def test_paper_html_auditors_keep_browser_tools_from_the_desktop_ui_package() -> None:
    ui = json.loads((PROJECT_ROOT / "desktop/ui/package.json").read_text(encoding="utf-8"))
    assert "@playwright/test" in ui["devDependencies"]
    assert "@axe-core/playwright" in ui["devDependencies"]
    audit = (PROJECT_ROOT / "scripts/audit-vln-guides-browser.mjs").read_text(encoding="utf-8")
    assert '"desktop", "ui", "package.json"' in audit
    assert 'require("@playwright/test")' in audit
    assert 'require("@axe-core/playwright")' in audit
    for retired in ("check-vln-guide-ui.mjs", "check-vln-code-ui.mjs"):
        assert not (PROJECT_ROOT / "scripts" / retired).exists()
