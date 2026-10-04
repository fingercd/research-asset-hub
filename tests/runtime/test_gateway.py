from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.asset_gateway.main import create_app
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from apps.asset_gateway.security import create_gateway_token, verify_gateway_token
from apps.asset_gateway.theme import THEME_VERSION, ThemeInjectionError
from packages.domain.models import (
    Asset,
    Base,
    Representation,
    RepresentationMount,
    Source,
)


@pytest.fixture
def gateway_runtime(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, DatabaseManager, Representation, Path, ApiSettings]]:
    root = tmp_path / "html"
    root.mkdir()
    page = root / "index.html"
    page.write_text("<!doctype html><title>Registered</title>", encoding="utf-8")
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="s" * 64,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with database.session() as session:
        source = Source(
            alias="demo",
            label="Demo",
            adapter="generic",
            root_path=str(root),
        )
        asset = Asset(asset_type="paper", title="Gateway Paper")
        session.add_all([source, asset])
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="html",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="index.html",
            mime_type="text/html",
        )
        session.add(representation)
        session.flush()
        session.expunge(representation)
    registry = SourceRegistry({("demo", "html"): root.resolve()})
    app = create_app(settings, database=database, registry=registry)
    with TestClient(app) as client:
        yield client, database, representation, page, settings
    engine.dispose()


def test_signed_gateway_session_range_head_and_host_security(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, _, representation, page, settings = gateway_runtime
    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="test",
    )
    session_response = client.get(
        f"/session/{representation.id}",
        params={"token": token},
        follow_redirects=False,
    )
    assert session_response.status_code == 303
    assert "httponly" in session_response.headers["set-cookie"].casefold()
    assert "samesite=lax" in session_response.headers["set-cookie"].casefold()
    assert session_response.headers["location"].startswith("/session-assets/")

    # Signed asset routes are intentionally independent of third-party cookies.
    client.cookies.clear()
    served = client.get(session_response.headers["location"])
    assert served.status_code == 200
    assert served.content == page.read_bytes()
    assert "default-src" in served.headers["content-security-policy"]
    assert "script-src 'none'" in served.headers["content-security-policy"]
    assert served.headers["x-research-hub-html-policy"] == "static"
    for origin in [settings.web_origin, *settings.desktop_origins]:
        assert origin in served.headers["content-security-policy"]

    cors = client.get(
        session_response.headers["location"],
        headers={"Origin": settings.web_origin},
    )
    assert cors.headers["access-control-allow-origin"] == settings.web_origin
    assert cors.headers["access-control-allow-credentials"] == "true"

    preflight = client.options(
        session_response.headers["location"],
        headers={
            "Origin": settings.web_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Range",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == settings.web_origin

    desktop_origin = settings.desktop_origins[0]
    desktop_preflight = client.options(
        session_response.headers["location"],
        headers={
            "Origin": desktop_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Range,If-Range",
        },
    )
    assert desktop_preflight.status_code == 200
    assert desktop_preflight.headers["access-control-allow-origin"] == desktop_origin
    assert desktop_preflight.headers["access-control-allow-credentials"] == "true"

    partial = client.get(
        session_response.headers["location"],
        headers={"Range": "bytes=0-8"},
    )
    assert partial.status_code == 206
    assert partial.headers["content-range"].startswith("bytes 0-8/")

    head = client.head(session_response.headers["location"])
    assert head.status_code == 200
    assert head.content == b""

    rejected_host = client.get("/health", headers={"Host": "evil.example"})
    assert rejected_host.status_code == 400


def test_desktop_signed_routes_serve_pdf_range_and_markdown_without_cookies(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, database, html_representation, page, settings = gateway_runtime
    pdf = page.parent / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n0123456789\n%%EOF\n")
    guide = page.parent / "guide.md"
    guide.write_text("# Local guide\n\nNo website required.\n", encoding="utf-8")
    with database.session() as session:
        html_item = session.get(Representation, html_representation.id)
        assert html_item is not None and html_item.source_id is not None
        pdf_asset = Asset(asset_type="paper", title="PDF")
        guide_asset = Asset(asset_type="note", title="Guide")
        session.add_all([pdf_asset, guide_asset])
        session.flush()
        pdf_representation = Representation(
            asset_id=pdf_asset.id,
            source_id=html_item.source_id,
            representation_type="pdf",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref=pdf.name,
            mime_type="application/pdf",
        )
        guide_representation = Representation(
            asset_id=guide_asset.id,
            source_id=html_item.source_id,
            representation_type="markdown",
            role="guide",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref=guide.name,
            mime_type="text/markdown",
        )
        session.add_all([pdf_representation, guide_representation])
        session.flush()
        pdf_id = pdf_representation.id
        guide_id = guide_representation.id

    desktop_origin = settings.desktop_origins[0]
    pdf_token = create_gateway_token(
        settings.gateway_secret,
        pdf_id,
        ttl_seconds=60,
        nonce="desktop-pdf",
    )
    pdf_session = client.get(
        f"/session/{pdf_id}",
        params={"token": pdf_token},
        follow_redirects=False,
    )
    client.cookies.clear()
    partial = client.get(
        pdf_session.headers["location"],
        headers={"Origin": desktop_origin, "Range": "bytes=0-7"},
    )
    assert partial.status_code == 206
    assert partial.content == pdf.read_bytes()[:8]
    assert partial.headers["access-control-allow-origin"] == desktop_origin
    assert partial.headers["accept-ranges"] == "bytes"

    guide_token = create_gateway_token(
        settings.gateway_secret,
        guide_id,
        ttl_seconds=60,
        nonce="desktop-guide",
    )
    guide_session = client.get(
        f"/session/{guide_id}",
        params={"token": guide_token},
        follow_redirects=False,
    )
    client.cookies.clear()
    markdown = client.get(
        guide_session.headers["location"],
        headers={
            "Origin": desktop_origin,
            "Accept": "text/markdown,text/plain;q=0.9",
        },
    )
    assert markdown.status_code == 200
    assert markdown.content == guide.read_bytes()
    assert markdown.headers["access-control-allow-origin"] == desktop_origin


def test_gateway_health_ignores_unused_historical_roots_and_still_serves_current_files(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, database, representation, page, settings = gateway_runtime
    healthy = client.get("/health")
    assert healthy.status_code == 200
    assert healthy.json()["source_roots"]["configured"] == 1
    assert healthy.json()["source_roots"]["verification"] == "registration_only"
    missing_root = page.parent / "missing-root"
    registry = SourceRegistry(
        {
            ("demo", "html"): page.parent.resolve(),
            ("demo", "pdf"): missing_root.resolve(),
        },
        workspace_root=page.parent,
    )
    app = create_app(settings, database=database, registry=registry)
    with TestClient(app) as current_client:
        health = current_client.get("/health")
        token = create_gateway_token(
            settings.gateway_secret, representation.id, ttl_seconds=60, nonce="historical-root"
        )
        signed = current_client.get(
            f"/session/{representation.id}", params={"token": token}, follow_redirects=False
        )
        assert signed.status_code == 303
        assert current_client.get(signed.headers["location"]).content == page.read_bytes()

    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "healthy"
    assert payload["registered_roots"] == 2
    assert payload["source_roots"]["configured"] == 2
    assert "missing_roots" not in payload["source_roots"]
    assert payload["source_roots"]["source_files_verified"] is False


@pytest.mark.parametrize("availability", ["available", "missing"])
def test_gateway_preserves_missing_file_and_unavailable_ledger_errors(
    gateway_runtime, availability
):
    client, database, representation, page, settings = gateway_runtime
    with database.session() as session:
        item = session.get(Representation, representation.id)
        item.availability = availability
    token = create_gateway_token(
        settings.gateway_secret, representation.id, ttl_seconds=60, nonce="actual-missing"
    )
    if availability == "available":
        signed = client.get(
            f"/session/{representation.id}", params={"token": token}, follow_redirects=False
        )
        assert signed.status_code == 303
        page.unlink()
        assert client.get(signed.headers["location"]).status_code == 404
    else:
        assert (
            client.get(f"/session/{representation.id}", params={"token": token}).status_code == 409
        )


def test_gateway_hub_presentation_injects_versioned_theme_and_preserves_source(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, _, representation, page, settings = gateway_runtime
    nested = page.parent / "details.html"
    nested.write_text(
        "<!doctype html><html><head><title>Details</title></head>"
        "<body><h1>Nested</h1></body></html>",
        encoding="utf-8",
    )
    source_content = page.read_bytes()
    source_stat = page.stat()
    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="hub-theme",
    )
    themed_session = client.get(
        f"/session/{representation.id}",
        params={"token": token, "presentation": "hub"},
        follow_redirects=False,
    )
    assert themed_session.status_code == 303
    assert "rah_gateway_presentation=hub" in themed_session.headers["set-cookie"]

    themed = client.get(themed_session.headers["location"])
    assert themed.status_code == 200
    assert f"/theme/{THEME_VERSION}.css" in themed.text
    assert f'data-research-hub-theme="{THEME_VERSION}"' in themed.text
    assert themed.headers["x-research-hub-presentation"] == "hub"
    assert themed.headers["x-research-hub-theme-version"] == THEME_VERSION
    themed_etag = themed.headers["etag"]
    unchanged = client.get(
        themed_session.headers["location"],
        headers={"If-None-Match": themed_etag},
    )
    assert unchanged.status_code == 304

    nested_response = client.get(f"/assets/{representation.id}/details.html")
    assert nested_response.status_code == 200
    assert f"/theme/{THEME_VERSION}.css" in nested_response.text
    stylesheet = client.get(f"/theme/{THEME_VERSION}.css")
    assert stylesheet.status_code == 200
    assert "--rah-accent" in stylesheet.text

    original_session = client.get(
        f"/session/{representation.id}",
        params={"token": token, "presentation": "original"},
        follow_redirects=False,
    )
    original = client.get(original_session.headers["location"])
    assert original.status_code == 200
    assert original.content == source_content
    assert original.headers["x-research-hub-presentation"] == "original"
    assert original.headers["etag"] != themed_etag
    assert page.read_bytes() == source_content
    assert page.stat().st_mtime_ns == source_stat.st_mtime_ns


def test_gateway_hub_presentation_falls_back_to_original_on_injection_failure(
    gateway_runtime: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, representation, page, settings = gateway_runtime

    def reject_theme(_: bytes) -> bytes:
        raise ThemeInjectionError("Synthetic parser failure.")

    monkeypatch.setattr("apps.asset_gateway.main.inject_theme_html", reject_theme)
    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="hub-theme-fallback",
    )
    session_response = client.get(
        f"/session/{representation.id}",
        params={"token": token, "presentation": "hub"},
        follow_redirects=False,
    )
    served = client.get(session_response.headers["location"])
    assert served.status_code == 200
    assert served.content == page.read_bytes()
    assert served.headers["x-research-hub-presentation"] == "original"
    assert served.headers["x-research-hub-theme-fallback"] == ("Synthetic parser failure.")


def test_gateway_hub_presentation_falls_back_when_document_csp_blocks_theme(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, _, representation, page, settings = gateway_runtime
    source_content = (
        b"<!doctype html><html><head>"
        b'<meta http-equiv="Content-Security-Policy" '
        b"content=\"default-src 'none'; style-src 'none'\">"
        b"<title>Strict CSP</title></head><body>Original</body></html>"
    )
    page.write_bytes(source_content)
    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="hub-theme-csp-fallback",
    )
    session_response = client.get(
        f"/session/{representation.id}",
        params={"token": token, "presentation": "hub"},
        follow_redirects=False,
    )

    served = client.get(session_response.headers["location"])

    assert served.status_code == 200
    assert served.content == source_content
    assert served.headers["x-research-hub-presentation"] == "original"
    assert "CSP" in served.headers["x-research-hub-theme-fallback"]
    assert f'data-research-hub-theme="{THEME_VERSION}"' not in served.text


def test_gateway_rejects_invalid_or_expired_token(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, _, representation, _, settings = gateway_runtime
    invalid = client.get(
        f"/session/{representation.id}",
        params={"token": "x" * 24},
        follow_redirects=False,
    )
    assert invalid.status_code == 403

    expired = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=1,
        nonce="expired",
        now=1,
    )
    with pytest.raises(ValueError, match="expired"):
        verify_gateway_token(
            settings.gateway_secret,
            expired,
            representation_id=representation.id,
            now=10,
        )


def test_registry_prevents_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    registry = SourceRegistry({("demo", "pdf"): root.resolve()})
    with pytest.raises(UnsafeAssetPath):
        registry.resolve("demo", "pdf", "../secret.pdf")


def test_registry_prevents_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("never exposed", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable on this host: {error}")

    registry = SourceRegistry({("demo", "repository"): root.resolve()})
    with pytest.raises(UnsafeAssetPath):
        registry.resolve("demo", "repository", "linked/secret.txt")


def test_registry_supports_chinese_spaces_long_paths_and_case(tmp_path: Path) -> None:
    root = tmp_path / "MiXeD Source"
    nested = root / ("中文 资料 " + "长" * 48)
    nested.mkdir(parents=True)
    paper = nested / "论文.PDF"
    paper.write_bytes(b"%PDF-1.4\n")
    registry = SourceRegistry({("demo", "pdf"): root.resolve()})

    resolved = registry.resolve(
        "demo",
        "pdf",
        f"{nested.name}/论文.pdf",
    )
    assert resolved.is_file()
    assert resolved.read_bytes().startswith(b"%PDF")


def test_gateway_rejects_encoded_traversal(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, _, representation, page, settings = gateway_runtime
    outside = page.parent.parent / "secret.txt"
    outside.write_text("never exposed", encoding="utf-8")
    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="encoded-traversal",
    )
    session_response = client.get(
        f"/session/{representation.id}",
        params={"token": token},
        follow_redirects=False,
    )
    assert session_response.status_code == 303

    single_encoded = client.get(
        f"/assets/{representation.id}/%2e%2e/secret.txt",
        follow_redirects=False,
    )
    assert single_encoded.status_code in {403, 404}
    assert single_encoded.content != outside.read_bytes()

    double_encoded = client.get(
        f"/assets/{representation.id}/%252e%252e/secret.txt",
        follow_redirects=False,
    )
    assert double_encoded.status_code in {403, 404}
    assert double_encoded.content != outside.read_bytes()


def test_file_mount_resolves_html_children_from_parent_directory(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, database, representation, page, settings = gateway_runtime
    stylesheet = page.parent / "site.css"
    stylesheet.write_text("body { color: #123456; }", encoding="utf-8")
    with database.session() as session:
        stored = session.get(Representation, representation.id)
        assert stored is not None and stored.source_id is not None
        session.add(
            RepresentationMount(
                representation_id=stored.id,
                mount_path="/",
                source_id=stored.source_id,
                root_key="html",
                path_ref="index.html",
                priority=100,
            )
        )

    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="file-mount-child",
    )
    session_response = client.get(
        f"/session/{representation.id}",
        params={"token": token},
        follow_redirects=False,
    )
    assert session_response.status_code == 303
    client.cookies.clear()
    signed_entrypoint = session_response.headers["location"]
    entrypoint = client.get(signed_entrypoint)
    assert entrypoint.status_code == 200
    child = client.get(f"{signed_entrypoint.rsplit('/', 1)[0]}/site.css")
    assert child.status_code == 200
    assert child.text == stylesheet.read_text(encoding="utf-8")


def test_strict_file_mount_blocks_unregistered_siblings_and_allows_explicit_mount(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, database, representation, page, settings = gateway_runtime
    sibling = page.parent / "unregistered.css"
    sibling.write_text("body { background: red; }", encoding="utf-8")
    static_directory = page.parent / "portal-static"
    static_directory.mkdir()
    allowed = static_directory / "allowed.css"
    allowed.write_text("body { background: green; }", encoding="utf-8")
    with database.session() as session:
        stored = session.get(Representation, representation.id)
        assert stored is not None and stored.source_id is not None
        stored.attributes = {"strict_mounts": True}
        session.add_all(
            [
                RepresentationMount(
                    representation_id=stored.id,
                    mount_path="/",
                    source_id=stored.source_id,
                    root_key="html",
                    path_ref="index.html",
                    priority=100,
                ),
                RepresentationMount(
                    representation_id=stored.id,
                    mount_path="/static",
                    source_id=stored.source_id,
                    root_key="html",
                    path_ref="portal-static",
                    priority=110,
                ),
            ]
        )

    token = create_gateway_token(
        settings.gateway_secret,
        representation.id,
        ttl_seconds=60,
        nonce="strict-mounts",
    )
    session_response = client.get(
        f"/session/{representation.id}",
        params={"token": token},
        follow_redirects=False,
    )
    entrypoint = client.get(session_response.headers["location"])
    assert entrypoint.status_code == 200
    assert entrypoint.content == page.read_bytes()

    registered_child = client.get(f"/assets/{representation.id}/static/allowed.css")
    assert registered_child.status_code == 200
    assert registered_child.text == allowed.read_text(encoding="utf-8")

    blocked_sibling = client.get(f"/assets/{representation.id}/unregistered.css")
    assert blocked_sibling.status_code == 404
    assert blocked_sibling.content != sibling.read_bytes()
    traversal = client.get(
        f"/assets/{representation.id}/static/%2e%2e/unregistered.css",
        follow_redirects=False,
    )
    assert traversal.status_code in {403, 404}
    assert traversal.content != sibling.read_bytes()


def test_directory_representation_lists_code_but_blocks_secrets(
    gateway_runtime: tuple[Any, ...],
) -> None:
    client, database, _, page, settings = gateway_runtime
    (page.parent / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (page.parent / ".env").write_text("TOKEN=never-serve\n", encoding="utf-8")
    with database.session() as session:
        source = session.scalar(select(Source).where(Source.alias == "demo"))
        assert source is not None
        asset = Asset(asset_type="repository", title="Code")
        session.add(asset)
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="repository",
            role="primary",
            materialization_kind="directory",
            availability="available",
            root_key="repository",
            path_ref=".",
        )
        session.add(representation)
        session.flush()
        representation_id = representation.id
    # A directory representation publishes its explicitly selected repository
    # root, rather than borrowing the fixture's HTML file registration.
    registry = SourceRegistry({("demo", "repository"): page.parent.resolve()})
    app = create_app(settings, database=database, registry=registry)
    with TestClient(app) as directory_client:
        token = create_gateway_token(
            settings.gateway_secret,
            representation_id,
            ttl_seconds=60,
            nonce="directory",
        )
        session_response = directory_client.get(
            f"/session/{representation_id}",
            params={"token": token},
            follow_redirects=False,
        )
        listing = directory_client.get(session_response.headers["location"])
        assert listing.status_code == 200
        assert "app.py" in listing.text
        assert ".env" not in listing.text
        blocked = directory_client.get(f"/assets/{representation_id}/.env")
        assert blocked.status_code == 403


def test_exact_root_directory_reference_requires_explicit_repository_registration(tmp_path: Path):
    root = tmp_path / "selected-directory"
    root.mkdir()
    registry = SourceRegistry({("demo", "html"): root, ("demo", "repository"): root})
    assert registry.resolve("demo", "repository", ".") == root
    for key, path in (
        ("html", "."),
        ("repository", ".."),
        ("repository", "./child"),
        ("repository", "../sibling"),
    ):
        with pytest.raises(UnsafeAssetPath):
            registry.resolve("demo", key, path)
