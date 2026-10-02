from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.asset_gateway.main import create_app
from apps.asset_gateway.registry import SourceRegistry
from apps.asset_gateway.security import create_gateway_token
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    RepresentationMount,
    Source,
    SourceRetirement,
    SourceRetirementRepresentationMap,
)


@pytest.fixture
def retirement_gateway(
    tmp_path: Path,
) -> Iterator[
    tuple[
        TestClient,
        DatabaseManager,
        Representation,
        Representation,
        Source,
        Asset,
        ApiSettings,
    ]
]:
    desktop_root = tmp_path / "desktop"
    core_root = tmp_path / "core"
    desktop_root.mkdir()
    core_root.mkdir()
    (desktop_root / "index.html").write_text(
        "<!doctype html><title>Retired desktop page</title>",
        encoding="utf-8",
    )
    (core_root / "index.html").write_text(
        "<!doctype html><title>Core successor page</title>",
        encoding="utf-8",
    )
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="s" * 64,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with database.session() as session:
        old_source = Source(
            alias="desktop-old",
            label="Desktop old",
            adapter="generic",
            root_path=r"C:\Users\ExampleUser\Desktop\private-library",
        )
        new_source = Source(
            alias="core-new",
            label="Core new",
            adapter="generic",
            root_path=str(core_root),
        )
        asset = Asset(asset_type="paper", title="Gateway retirement")
        session.add_all([old_source, new_source, asset])
        session.flush()
        retired = Representation(
            asset_id=asset.id,
            source_id=old_source.id,
            representation_type="html",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="index.html",
            mime_type="text/html",
        )
        successor = Representation(
            asset_id=asset.id,
            source_id=new_source.id,
            representation_type="html",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="index.html",
            mime_type="text/html",
        )
        session.add_all([retired, successor])
        session.flush()
        for item in (retired, successor, old_source, asset):
            session.expunge(item)
    registry = SourceRegistry(
        {
            ("desktop-old", "html"): desktop_root.resolve(),
            ("core-new", "html"): core_root.resolve(),
        }
    )
    app = create_app(settings, database=database, registry=registry)
    with TestClient(app) as client:
        yield client, database, retired, successor, old_source, asset, settings
    engine.dispose()


def _retire_source(
    database: DatabaseManager,
    *,
    source: Source,
    asset: Asset,
    retired: Representation,
    successor: Representation,
) -> None:
    digest = "d" * 64
    with database.session() as session:
        backup = BackupRun(destination_label="gateway-retirement-test")
        session.add(backup)
        session.flush()
        retirement = SourceRetirement(
            source_id=source.id,
            backup_id=backup.id,
            payload={"source_id": str(source.id)},
            payload_digest="e" * 64,
            frozen_manifest_sha256=digest,
            frozen_source_config_sha256=digest,
            record_set_digest=digest,
            representation_set_digest=digest,
            mount_set_digest=digest,
            collection_digest=digest,
            signature_algorithm="hmac-sha256",
            signer_key_id="test",
            signature="test-signature",
            applied_by="pytest",
        )
        session.add(retirement)
        session.flush()
        session.add(
            SourceRetirementRepresentationMap(
                retirement_id=retirement.id,
                retired_representation_id=retired.id,
                successor_representation_id=successor.id,
                asset_id=asset.id,
                content_digest=digest,
                retired_representation_digest=digest,
                successor_representation_digest=digest,
            )
        )


def _assert_path_free_retired_problem(response: Any, successor: Representation) -> None:
    assert response.status_code == 410
    assert response.json()["code"] == "representation_retired"
    assert response.json()["successor_representation_id"] == str(successor.id)
    assert "Desktop" not in response.text
    assert "private-library" not in response.text


def test_gateway_rechecks_retirement_at_session_and_every_request(
    retirement_gateway: tuple[Any, ...],
) -> None:
    client, database, retired, successor, source, asset, settings = retirement_gateway
    token = create_gateway_token(
        settings.gateway_secret,
        retired.id,
        ttl_seconds=60,
        nonce="retirement-test",
    )

    established = client.get(
        f"/session/{retired.id}",
        params={"token": token},
        follow_redirects=False,
    )
    assert established.status_code == 303

    _retire_source(
        database,
        source=source,
        asset=asset,
        retired=retired,
        successor=successor,
    )

    existing_session_request = client.get(established.headers["location"])
    _assert_path_free_retired_problem(existing_session_request, successor)

    new_session = client.get(
        f"/session/{retired.id}",
        params={"token": token},
        follow_redirects=False,
    )
    _assert_path_free_retired_problem(new_session, successor)


def test_gateway_never_serves_a_mount_from_a_retired_source(
    retirement_gateway: tuple[Any, ...],
) -> None:
    client, database, retired, successor, source, asset, settings = retirement_gateway
    with database.session() as session:
        successor_row = session.get(Representation, successor.id)
        assert successor_row is not None
        successor_row.attributes = {"strict_mounts": True}
        session.add(
            RepresentationMount(
                representation_id=successor.id,
                mount_path="/",
                source_id=source.id,
                root_key="html",
                path_ref="index.html",
                priority=10,
            )
        )

    token = create_gateway_token(
        settings.gateway_secret,
        successor.id,
        ttl_seconds=60,
        nonce="mount-retirement-test",
    )
    established = client.get(
        f"/session/{successor.id}",
        params={"token": token},
        follow_redirects=False,
    )
    assert established.status_code == 303
    before = client.get(established.headers["location"])
    assert before.status_code == 200
    assert b"Retired desktop page" in before.content

    _retire_source(
        database,
        source=source,
        asset=asset,
        retired=retired,
        successor=successor,
    )

    after = client.get(established.headers["location"])
    assert after.status_code == 404
    assert b"Retired desktop page" not in after.content
