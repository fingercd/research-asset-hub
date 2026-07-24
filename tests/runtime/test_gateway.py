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

    served = client.get(session_response.headers["location"])
    assert served.status_code == 200
    assert served.content == page.read_bytes()
    assert "default-src" in served.headers["content-security-policy"]
    assert "script-src 'none'" in served.headers["content-security-policy"]
    assert served.headers["x-research-hub-html-policy"] == "static"

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
    child = client.get(f"/assets/{representation.id}/site.css")
    assert child.status_code == 200
    assert child.text == stylesheet.read_text(encoding="utf-8")


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
            root_key="html",
            path_ref=".",
        )
        session.add(representation)
        session.flush()
        representation_id = representation.id
    token = create_gateway_token(
        settings.gateway_secret,
        representation_id,
        ttl_seconds=60,
        nonce="directory",
    )
    session_response = client.get(
        f"/session/{representation_id}",
        params={"token": token},
        follow_redirects=False,
    )
    listing = client.get(session_response.headers["location"])
    assert listing.status_code == 200
    assert "app.py" in listing.text
    assert ".env" not in listing.text
    blocked = client.get(f"/assets/{representation_id}/.env")
    assert blocked.status_code == 403
