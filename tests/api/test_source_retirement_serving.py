from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    Source,
    SourceRetirement,
    SourceRetirementRepresentationMap,
    SourceRetirementRollback,
)


@pytest.fixture
def retirement_api(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, DatabaseManager, ApiSettings]]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="r" * 64,
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
    )
    settings.sources_config_path.write_text(
        "schema_version: 1\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )
    settings.taxonomy_config_path.write_text(
        "schema_version: 1\ndomains:\n  - id: agent\n    label: Agent\n",
        encoding="utf-8",
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    app = create_app(settings, database=database)
    with TestClient(app) as client:
        yield client, database, settings
    engine.dispose()


def _seed_representations(
    database: DatabaseManager,
) -> tuple[Asset, Source, Representation, Representation]:
    with database.session() as session:
        old_source = Source(
            alias="desktop-old",
            label="Desktop old",
            adapter="generic_manifest",
            root_path=r"C:\Users\ExampleUser\Desktop\private-library",
        )
        new_source = Source(
            alias="core-new",
            label="Core successor",
            adapter="generic_manifest",
            root_path=r"D:\ResearchLibrary\归拢",
        )
        asset = Asset(
            asset_type="paper",
            title="Retirement Scope Paper",
            domain_slugs=["agent"],
        )
        session.add_all([old_source, new_source, asset])
        session.flush()
        retired = Representation(
            asset_id=asset.id,
            source_id=old_source.id,
            representation_type="pdf",
            role="primary",
            availability="available",
            path_ref="Desktop-only evidence.pdf",
            attributes={"private_location": r"C:\Users\ExampleUser\Desktop\secret.pdf"},
        )
        successor = Representation(
            asset_id=asset.id,
            source_id=new_source.id,
            representation_type="pdf",
            role="primary",
            availability="available",
            path_ref="papers/successor.pdf",
        )
        session.add_all([retired, successor])
        session.flush()
        for item in (asset, old_source, retired, successor):
            session.expunge(item)
        return asset, old_source, retired, successor


def _retire(
    database: DatabaseManager,
    *,
    asset: Asset,
    source: Source,
    retired: Representation,
    successor: Representation,
) -> SourceRetirement:
    digest = "a" * 64
    with database.session() as session:
        backup = BackupRun(destination_label="retirement-test")
        session.add(backup)
        session.flush()
        retirement = SourceRetirement(
            source_id=source.id,
            backup_id=backup.id,
            payload={"source_id": str(source.id)},
            payload_digest="b" * 64,
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
        session.flush()
        session.expunge(retirement)
        return retirement


def _rollback(database: DatabaseManager, retirement: SourceRetirement) -> None:
    with database.session() as session:
        backup = BackupRun(destination_label="rollback-test")
        session.add(backup)
        session.flush()
        session.add(
            SourceRetirementRollback(
                retirement_id=retirement.id,
                backup_id=backup.id,
                payload={"retirement_id": str(retirement.id)},
                payload_digest="c" * 64,
                signature_algorithm="hmac-sha256",
                signer_key_id="test",
                signature="test-signature",
                reason="pytest rollback",
                rolled_back_by="pytest",
            )
        )


def _install_search(
    monkeypatch: pytest.MonkeyPatch,
    asset: Asset,
    representation: Representation,
) -> None:
    class SearchService:
        def search(self, _query: str, **_options: Any) -> SimpleNamespace:
            return SimpleNamespace(
                hits=[
                    SimpleNamespace(
                        document=SimpleNamespace(
                            asset_id=str(asset.id),
                            text="Desktop-only evidence that must be filtered.",
                            kind="representation",
                            metadata={
                                "representation_id": str(representation.id),
                                "source_id": str(representation.source_id),
                            },
                        ),
                        score=1.0,
                        source="keyword",
                        exact_match=True,
                    )
                ],
                degraded=False,
                errors=(),
                mode=SimpleNamespace(value="keyword"),
            )

    monkeypatch.setattr(
        "apps.api.routes_v1._search_runtime",
        lambda _request: SimpleNamespace(service=SearchService()),
    )


def test_retirement_filters_ui_and_returns_path_free_410(
    retirement_api: tuple[TestClient, DatabaseManager, ApiSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, settings = retirement_api
    asset, source, retired, successor = _seed_representations(database)
    _retire(
        database,
        asset=asset,
        source=source,
        retired=retired,
        successor=successor,
    )

    detail = client.get(f"/api/v1/assets/{asset.id}")
    assert detail.status_code == 200
    assert [item["id"] for item in detail.json()["representations"]] == [
        str(successor.id)
    ]

    listed = client.get("/api/v1/assets", params={"include": "representations"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"][0]["representations"]] == [
        str(successor.id)
    ]

    representations = client.get(f"/api/v1/assets/{asset.id}/representations")
    assert [item["id"] for item in representations.json()["items"]] == [
        str(successor.id)
    ]

    audit = client.get(
        f"/api/v1/assets/{asset.id}/representations",
        params={"include_retired": "true"},
    )
    assert audit.status_code == 200
    retired_item = next(item for item in audit.json()["items"] if item.get("retired"))
    assert retired_item["successor_representation_id"] == str(successor.id)
    assert retired_item["path_ref"] is None
    assert retired_item["external_url"] is None
    assert retired_item["open_actions"] == []
    assert "Desktop" not in audit.text
    assert "private-library" not in audit.text

    grouped = client.get("/api/v1/search", params={"group_by": "paper"})
    quick_pdf = next(
        item
        for item in grouped.json()["items"][0]["quick_actions"]
        if item["kind"] == "pdf"
    )
    assert quick_pdf["representation_id"] == str(successor.id)

    opened = client.post(f"/api/v1/representations/{retired.id}/open-actions")
    assert opened.status_code == 410
    assert opened.json()["code"] == "representation_retired"
    assert opened.json()["errors"][0]["successor_representation_id"] == str(
        successor.id
    )
    assert opened.headers["X-Successor-Representation-ID"] == str(successor.id)
    assert "Desktop" not in opened.text

    _install_search(monkeypatch, asset, retired)
    search = client.get("/api/v1/search", params={"q": "Desktop-only evidence"})
    assert search.status_code == 200
    assert search.json()["items"] == []

    settings.sources_config_path.write_text(
        "schema_version: 1\n"
        "workspace_root: .\n"
        "sources:\n"
        "  - id: desktop-old\n"
        "    label: Desktop old\n"
        "    adapter: generic_manifest\n"
        "  - id: core-new\n"
        "    label: Core new\n"
        "    adapter: generic_manifest\n",
        encoding="utf-8",
    )
    visible_source_ids = {
        item["id"] for item in client.get("/api/v1/settings").json()["sources"]
    }
    assert visible_source_ids == {"core-new"}


def test_rollback_restores_normal_serving_scope(
    retirement_api: tuple[TestClient, DatabaseManager, ApiSettings],
) -> None:
    client, database, _ = retirement_api
    asset, source, retired, successor = _seed_representations(database)

    before = client.get(f"/api/v1/assets/{asset.id}")
    assert {item["id"] for item in before.json()["representations"]} == {
        str(retired.id),
        str(successor.id),
    }

    retirement = _retire(
        database,
        asset=asset,
        source=source,
        retired=retired,
        successor=successor,
    )
    assert client.post(f"/api/v1/representations/{retired.id}/open-actions").status_code == 410

    _rollback(database, retirement)
    restored = client.get(f"/api/v1/assets/{asset.id}")
    assert {item["id"] for item in restored.json()["representations"]} == {
        str(retired.id),
        str(successor.id),
    }
    assert client.post(f"/api/v1/representations/{retired.id}/open-actions").status_code == 200
