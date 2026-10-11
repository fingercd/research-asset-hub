"""Catalog contracts cover bounded materialization and SQL selection semantics."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import (
    Annotation,
    Asset,
    Base,
    ExternalIdentifierModel,
    ReadingState,
    Relation,
    Source,
    SourceRecord,
)
from packages.domain.platform_models import SourceRecordAsset
from packages.indexing.models import SearchDocument, SearchHit


def runtime(tmp_path: Path):
    sources = tmp_path / "sources.yaml"
    sources.write_text("schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        "domains:\n  - id: alpha\n    label: Alpha\n"
        "  - id: image-editing\n    label: Image\n"
        "  - id: video-generation\n    label: Video\n"
        "  - id: embodied-ai-compression\n    label: Compression\n"
        "  - id: generation\n    hidden_from_catalog: true\n",
        encoding="utf-8",
    )
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        sources_config_path=sources,
        taxonomy_config_path=taxonomy,
        domain_portal_config_path=tmp_path / "unconfigured-portals",
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    return settings, engine, database


def test_catalog_loads_only_page_metadata_and_keeps_full_facets(tmp_path):
    settings, engine, database = runtime(tmp_path)
    with database.session() as session:
        session.add_all(
            [
                Asset(
                    asset_type="paper",
                    title=f"Paper {index:03d}",
                    year=2025,
                    domain_slugs=["alpha"],
                    tags=["shared"],
                    content="original body " * 10000,
                    analysis={"body": "analysis " * 10000},
                    attributes={
                        "release_status": {"code_open": "official_open"},
                        "code_availability": {"body": "long audit " * 1000},
                    },
                )
                for index in range(73)
            ]
        )
        session.add(
            Asset(
                asset_type="collection",
                title="Hidden",
                domain_slugs=["alpha"],
                attributes={"system_hidden": "TRUE"},
            )
        )
    statements = []

    def record(_connection, _cursor, statement, parameters, *_rest):
        statements.append((statement.lower(), parameters))

    with TestClient(create_app(settings, database=database)) as client:
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.get("/api/v1/search", params={"domain": "alpha", "group_by": "paper"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 73
            assert len(body["items"]) == 30
            assert body["facets"]["types"] == {"paper": 73}
            assert body["facets"]["tags"] == {"shared": 73}
            assert body["facets"]["domains"] == {"alpha": 73}
            assert all(
                "content" not in unit["primary_asset"] and "analysis" not in unit["primary_asset"]
                for unit in body["items"]
            )
            assert all(
                unit["primary_asset"]["attributes"]
                == {"release_status": {"code_open": "official_open"}}
                for unit in body["items"]
            )
            assert all(
                "abstract" not in match and "summary" not in match
                for unit in body["items"]
                for match in unit["matched_assets"]
            )
            assert all(
                "assets.content" not in sql and "assets.analysis" not in sql
                for sql, _ in statements
            )
            asset_loads = [(sql, params) for sql, params in statements if "assets.summary" in sql]
            assert len(asset_loads) == 1
            assert "where assets.id in" in asset_loads[0][0]
            assert (
                sum(isinstance(value, str) and len(value) == 32 for value in asset_loads[0][1])
                == 30
            )
            assert any(
                "limit" in sql and "offset" in sql and "order by" in sql for sql, _ in statements
            )
            statements.clear()
            domains = client.get("/api/v1/domains").json()["items"]
            alpha = next(domain for domain in domains if domain["slug"] == "alpha")
            assert alpha["paper_count"] == 73
            assert len(alpha["recent_assets"]) == 5
            assert all(
                "assets.content" not in sql and "assets.analysis" not in sql
                for sql, _ in statements
            )
            asset_loads = [
                (sql, params)
                for sql, params in statements
                if "assets.summary" in sql and "where assets.id in" in sql
            ]
            assert len(asset_loads) == 1
            assert (
                sum(isinstance(value, str) and len(value) == 32 for value in asset_loads[0][1]) == 5
            )
            without_facets = client.get(
                "/api/v1/search",
                params={"domain": "alpha", "group_by": "paper", "include_facets": False},
            )
            assert without_facets.status_code == 200, without_facets.text
            assert without_facets.json()["total"] == 73
            assert "facets" not in without_facets.json()
            assert without_facets.json()["items"] == body["items"]
        finally:
            event.remove(engine, "before_cursor_execute", record)
    engine.dispose()


def test_sql_domains_preserve_source_paper_inheritance_and_counts(tmp_path):
    settings, engine, database = runtime(tmp_path)
    with database.session() as session:
        source = Source(alias="topics", label="Topics", adapter="generation", root_path=".")
        paper = Asset(
            asset_type="paper",
            title="Opaque paper",
            domain_slugs=["generation"],
            attributes={"category": "video_core"},
        )
        repository = Asset(
            asset_type="repository", title="Opaque repository", domain_slugs=["generation", "alpha"]
        )
        image = Asset(
            asset_type="paper",
            title="Misleading video title",
            domain_slugs=["generation"],
            attributes={"category": "image_core"},
        )
        navigation = Asset(
            asset_type="paper", title="Navigation", domain_slugs=["navigation-compression"]
        )
        session.add_all([source, paper, repository, image, navigation])
        session.flush()
        record = SourceRecord(source_id=source.id, source_record_id="video", asset_id=paper.id)
        session.add(record)
        session.flush()
        session.add_all(
            [
                SourceRecordAsset(source_record_id=record.id, asset_id=paper.id, role="primary"),
                SourceRecordAsset(
                    source_record_id=record.id, asset_id=repository.id, role="repository"
                ),
                ReadingState(asset_id=paper.id, user_id="local", status="reading"),
                Annotation(asset_id=paper.id, summary="One"),
                Annotation(asset_id=paper.id, summary="Two"),
            ]
        )
        paper_id = str(paper.id)
    with TestClient(create_app(settings, database=database)) as client:
        domains = {
            domain["slug"]: domain for domain in client.get("/api/v1/domains").json()["items"]
        }
        assert "generation" not in domains
        assert domains["video-generation"]["asset_count"] == 2
        assert domains["video-generation"]["paper_count"] == 1
        assert domains["video-generation"]["research_unit_count"] == 2
        assert domains["video-generation"]["reading_counts"] == {
            "unread": 1,
            "reading": 1,
            "read": 0,
        }
        assert domains["video-generation"]["annotation_count"] == 2
        assert domains["image-editing"]["paper_count"] == 1
        assert domains["embodied-ai-compression"]["paper_count"] == 1
        browse = client.get(
            "/api/v1/search", params={"domain": "video-generation", "group_by": "paper"}
        )
        assert browse.status_code == 200, browse.text
        assert browse.json()["total"] == 1
        assert browse.json()["items"][0]["unit_id"] == paper_id
        raw = client.get(
            "/api/v1/search", params={"domain": "video-generation", "type": "repository"}
        )
        assert raw.status_code == 200, raw.text
        assert raw.json()["items"][0]["domain_slugs"] == ["alpha", "video-generation"]
        ordinary = client.get("/api/v1/domains/alpha/portal")
        assert ordinary.status_code == 200, ordinary.text
        assert ordinary.json()["portal"] is None
    engine.dispose()


def test_web_metadata_fallback_excludes_bodies_authors_and_tags(tmp_path, monkeypatch):
    settings, engine, database = runtime(tmp_path)

    class OfflineSearch:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("test offline search")

    monkeypatch.setattr(
        "apps.api.routes_v1._search_runtime",
        lambda _request: SimpleNamespace(service=OfflineSearch()),
    )
    with database.session() as session:
        session.add(
            Asset(
                asset_type="paper",
                title="Unrelated",
                authors=["needle"],
                tags=["needle"],
                summary="needle",
                content="needle",
                analysis={"body": "needle"},
            )
        )
        relevant = Asset(asset_type="paper", title="Title needle", abstract="Metadata needle")
        identifier = Asset(asset_type="paper", title="Identified paper")
        session.add_all([relevant, identifier])
        session.flush()
        session.add(
            ExternalIdentifierModel(
                asset_id=identifier.id,
                scheme="doi",
                value="10.1234/exact-id",
                canonical="10.1234/exact-id",
            )
        )
        relevant_id = str(relevant.id)
        identifier_id = str(identifier.id)
    with TestClient(create_app(settings, database=database)) as client:
        for grouping in ["none", "paper"]:
            result = client.get("/api/v1/search", params={"q": "needle", "group_by": grouping})
            assert result.status_code == 200, result.text
            assert result.json()["total"] == 1
            item = result.json()["items"][0]
            assert (item["unit_id"] if grouping == "paper" else item["id"]) == relevant_id
        result = client.get("/api/v1/search", params={"q": "10.1234/exact-id"})
        assert result.status_code == 200, result.text
        assert result.json()["items"][0]["id"] == identifier_id
    engine.dispose()


def test_linked_repository_remains_searchable_when_paper_fails_unit_filter(tmp_path, monkeypatch):
    settings, engine, database = runtime(tmp_path)

    class OfflineSearch:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("test offline search")

    monkeypatch.setattr(
        "apps.api.routes_v1._search_runtime",
        lambda _request: SimpleNamespace(service=OfflineSearch()),
    )
    with database.session() as session:
        paper = Asset(
            asset_type="paper", title="Paper", domain_slugs=["alpha"], tags=["paper-only"]
        )
        repository = Asset(
            asset_type="repository",
            title="Needle repository",
            domain_slugs=["alpha"],
            tags=["repository-only"],
        )
        session.add_all([paper, repository])
        session.flush()
        session.add(
            Relation(
                subject_asset_id=paper.id,
                object_asset_id=repository.id,
                relation_type="implemented_by",
                confirmation_status="confirmed",
            )
        )
        paper_id, repository_id = str(paper.id), str(repository.id)
    with TestClient(create_app(settings, database=database)) as client:
        grouped = client.get("/api/v1/search", params={"q": "needle", "group_by": "paper"})
        assert grouped.status_code == 200, grouped.text
        assert grouped.json()["total"] == 1
        assert grouped.json()["items"][0]["unit_id"] == paper_id
        filtered = client.get(
            "/api/v1/search", params={"q": "needle", "group_by": "paper", "tag": "repository-only"}
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["unit_id"] == repository_id
    engine.dispose()


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize(
    ("date_filters", "expected_units"),
    [
        ({"date_from": "2025-01-01"}, {"old-repo", "current-paper", "future-paper", "independent"}),
        ({"date_to": "2025-12-31"}, {"old-paper", "current-paper", "future-repo", "independent"}),
        (
            {"date_from": "2025-01-01", "date_to": "2025-12-31"},
            {"old-repo", "current-paper", "future-repo", "independent"},
        ),
    ],
)
def test_grouped_date_filters_keep_repository_when_paper_is_outside_range(
    tmp_path, monkeypatch, indexed, date_filters, expected_units
):
    settings, engine, database = runtime(tmp_path)

    class OfflineSearch:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("test offline search")

    monkeypatch.setattr(
        "apps.api.routes_v1._search_runtime",
        lambda _request: SimpleNamespace(service=OfflineSearch()),
    )
    with database.session() as session:
        units = {}
        for label, published in [
            ("old", "2020-06-01"),
            ("current", "2025-06-01"),
            ("future", "2026-06-01"),
        ]:
            paper = Asset(
                asset_type="paper",
                title=f"{label} paper",
                year=int(published[:4]),
                authors=["Ada"],
                domain_slugs=["alpha"],
                tags=["shared"],
                attributes={"first_public_date": published},
            )
            repository = Asset(
                asset_type="repository",
                title=f"Needle {label} repo",
                year=2025,
                authors=["Ada"],
                domain_slugs=["alpha"],
                tags=["shared"],
                attributes={"first_public_date": "2025-06-01"},
            )
            session.add_all([paper, repository])
            session.flush()
            session.add(
                Relation(
                    subject_asset_id=paper.id,
                    object_asset_id=repository.id,
                    relation_type="implemented_by",
                    confirmation_status="confirmed",
                )
            )
            units[f"{label}-paper"] = str(paper.id)
            units[f"{label}-repo"] = str(repository.id)
        independent = Asset(
            asset_type="repository",
            title="Needle independent",
            year=2025,
            authors=["Ada"],
            domain_slugs=["alpha"],
            tags=["shared"],
            attributes={"first_public_date": "2025-06-01"},
        )
        excluded = Asset(
            asset_type="repository",
            title="Needle excluded",
            year=2025,
            authors=["Other"],
            domain_slugs=["image-editing"],
            tags=["other"],
            attributes={"first_public_date": "2025-06-01"},
        )
        session.add_all([independent, excluded])
        session.flush()
        units["independent"] = str(independent.id)
    if indexed:
        hits = [
            SearchHit(
                document=SearchDocument(
                    document_id=asset_id,
                    asset_id=asset_id,
                    kind="repository",
                    title=f"Needle {label}",
                    text="Needle repository README",
                ),
                score=1 / (60 + rank),
                rank=rank,
                source="keyword",
            )
            for rank, (label, asset_id) in enumerate(
                [
                    (label, asset_id)
                    for label, asset_id in units.items()
                    if label.endswith("repo") or label == "independent"
                ],
                start=1,
            )
        ]
        search_result = SimpleNamespace(
            hits=hits, degraded=False, errors=[], mode=SimpleNamespace(value="keyword")
        )
        monkeypatch.setattr(
            "apps.api.routes_v1._search_runtime",
            lambda _request: SimpleNamespace(
                service=SimpleNamespace(search=lambda *_args, **_kwargs: search_result)
            ),
        )
    with TestClient(create_app(settings, database=database)) as client:
        filters = {"q": "needle", "group_by": "paper", "domain": "alpha", **date_filters}
        result = client.get("/api/v1/search", params=filters)
        assert result.status_code == 200, result.text
        assert result.json()["degraded"] is not indexed
        assert result.json()["total"] == len(expected_units)
        assert {item["unit_id"] for item in result.json()["items"]} == {
            units[label] for label in expected_units
        }
        ungrouped = client.get(
            "/api/v1/search", params={**filters, "group_by": "none", "type": "repository"}
        )
        assert ungrouped.status_code == 200, ungrouped.text
        assert {item["id"] for item in ungrouped.json()["items"]} == {
            asset_id
            for label, asset_id in units.items()
            if label.endswith("repo") or label == "independent"
        }
        # Pagination and facets use the same qualified units after all filters.
        monkeypatch.setattr("apps.api.routes.assets.PAGE_SIZE", 2)
        paged_filters = {
            **filters,
            "author": "Ada",
            "tag": "shared",
            "status": "unread",
            "sort": "date_desc",
        }
        first = client.get("/api/v1/search", params=paged_filters)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["degraded"] is not indexed
        assert first_body["total"] == len(expected_units)
        assert first_body["facets"]["tags"] == {"shared": len(expected_units)}
        assert first_body["facets"]["reading_status"] == {"unread": len(expected_units)}
        assert len(first_body["items"]) == 2
        assert first_body["next_cursor"]
        second = client.get(
            "/api/v1/search", params={**paged_filters, "cursor": first_body["next_cursor"]}
        )
        assert second.status_code == 200, second.text
        assert second.json()["degraded"] is not indexed
        assert second.json()["next_cursor"] is None
        assert {item["unit_id"] for item in first_body["items"] + second.json()["items"]} == {
            units[label] for label in expected_units
        }
        papers = client.get("/api/v1/search", params={**filters, "type": "paper"})
        assert papers.status_code == 200, papers.text
        expected_papers = {units[label] for label in expected_units if label.endswith("paper")}
        assert {item["unit_id"] for item in papers.json()["items"]} == expected_papers
    engine.dispose()
