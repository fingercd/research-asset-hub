from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Asset, Base, Representation, Source
from packages.integrations.html_audit import (
    DynamicPageCheck,
    DynamicProbeBatch,
    audit_registered_html,
)


@pytest.fixture
def html_runtime(
    tmp_path: Path,
) -> Iterator[
    tuple[DatabaseManager, SourceRegistry, Representation, Path]
]:
    root = tmp_path / "registered"
    root.mkdir()
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
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
        asset = Asset(asset_type="paper", title="HTML audit")
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
    yield (
        database,
        SourceRegistry({("demo", "html"): root.resolve()}),
        representation,
        root,
    )
    engine.dispose()


def test_static_audit_classifies_dependencies_and_redacts_urls(
    html_runtime: tuple[DatabaseManager, SourceRegistry, Representation, Path],
) -> None:
    database, registry, _, root = html_runtime
    (root / "style.css").write_text(
        '@import url("https://cdn.example.test/theme.css?api_key=never");\n'
        "body { background-image: url(missing.png); }\n",
        encoding="utf-8",
    )
    (root / "index.html").write_text(
        """
        <!doctype html>
        <link rel="stylesheet" href="style.css">
        <script>fetch("https://api.example.test/data?token=never")</script>
        <a href="https://user:password@example.test/paper?token=never">paper</a>
        <img src="../outside.png">
        """,
        encoding="utf-8",
    )

    report = audit_registered_html(database, registry)
    rendered = cast(dict[str, Any], report.to_dict())
    page = rendered["representations"][0]
    assert page["static"]["status"] == "audited"
    assert page["static"]["inferred_policy"] == "network_required"
    dependencies = page["static"]["dependencies"]
    assert any(
        item["classification"] == "external_network"
        and item["context"] == "stylesheet.url"
        for item in dependencies
    )
    assert any(item["classification"] == "unsafe_local" for item in dependencies)
    serialized = json.dumps(rendered, ensure_ascii=False)
    assert "token=never" not in serialized
    assert "user:password" not in serialized
    assert str(root.resolve()) not in serialized


def test_static_audit_infers_trusted_local_for_offline_script(
    html_runtime: tuple[DatabaseManager, SourceRegistry, Representation, Path],
) -> None:
    database, registry, _, root = html_runtime
    (root / "index.html").write_text(
        "<!doctype html><button onclick=\"document.title='ok'\">Run</button>",
        encoding="utf-8",
    )
    report = audit_registered_html(database, registry)
    page = report.pages[0]
    assert page.inferred_policy == "trusted_local"
    assert page.inline_event_handler_count == 1
    assert "declared_policy_mismatch" in page.risk_flags


class _FakeProbe:
    def probe(
        self,
        representation_ids: Sequence[UUID],
        *,
        api_origin: str,
        timeout_seconds: float,
    ) -> DynamicProbeBatch:
        assert api_origin == "http://127.0.0.1:8767"
        assert timeout_seconds == 3
        representation_id = representation_ids[0]
        return DynamicProbeBatch(
            "completed",
            None,
            {
                representation_id: DynamicPageCheck(
                    representation_id,
                    "completed",
                    policy="static",
                    csp="script-src 'none'",
                    console_errors=("blocked script",),
                    failed_resources=(
                        {
                            "url": "http://127.0.0.1:8766/missing.js",
                            "error": "HTTP 404",
                        },
                    ),
                )
            },
        )


def test_optional_dynamic_probe_is_attached_without_local_paths(
    html_runtime: tuple[DatabaseManager, SourceRegistry, Representation, Path],
) -> None:
    database, registry, representation, root = html_runtime
    (root / "index.html").write_text("<!doctype html><title>Safe</title>", encoding="utf-8")
    report = audit_registered_html(
        database,
        registry,
        dynamic_sample_size=1,
        dynamic_probe=_FakeProbe(),
        timeout_seconds=3,
    )
    assert report.dynamic_probe_status == "completed"
    assert report.pages[0].dynamic == DynamicPageCheck(
        representation.id,
        "completed",
        policy="static",
        csp="script-src 'none'",
        console_errors=("blocked script",),
        failed_resources=(
            {
                "url": "http://127.0.0.1:8766/missing.js",
                "error": "HTTP 404",
            },
        ),
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8767",
        "http://example.test:8767",
        "http://user:password@127.0.0.1:8767",
    ],
)
def test_dynamic_probe_origin_must_be_plain_loopback(
    html_runtime: tuple[DatabaseManager, SourceRegistry, Representation, Path],
    origin: str,
) -> None:
    database, registry, _, root = html_runtime
    (root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        audit_registered_html(database, registry, api_origin=origin)


def test_unavailable_dynamic_probe_is_explicitly_unverified(
    html_runtime: tuple[DatabaseManager, SourceRegistry, Representation, Path],
) -> None:
    database, registry, _, root = html_runtime
    (root / "index.html").write_text("<!doctype html>", encoding="utf-8")

    class UnavailableProbe:
        def probe(
            self,
            representation_ids: Sequence[UUID],
            *,
            api_origin: str,
            timeout_seconds: float,
        ) -> DynamicProbeBatch:
            return DynamicProbeBatch("unavailable", "browser unavailable", {})

    report = audit_registered_html(
        database,
        registry,
        dynamic_sample_size=1,
        dynamic_probe=UnavailableProbe(),
    )
    rendered: dict[str, Any] = report.to_dict()
    assert rendered["status"] == "completed_with_unverified_items"
    assert rendered["summary"]["dynamic_probe_status"] == "unavailable"
    assert rendered["representations"][0]["dynamic"]["status"] == "unverified"
