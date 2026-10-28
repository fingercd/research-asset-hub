"""Catalog contracts cover bounded materialization and SQL selection semantics."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

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
    Representation,
    Source,
    SourceRecord,
)
from packages.domain.platform_models import SourceRecordAsset
from packages.domain.workflow_models import ReadingPosition
from packages.indexing.models import SearchDocument, SearchHit


@pytest.mark.parametrize("group_by", ["none", "paper"])
@pytest.mark.parametrize("include_facets", [True, False])
def test_venue_year_are_exact_sql_filters_before_all_pages(tmp_path, group_by, include_facets):
    settings, engine, database = runtime(tmp_path)
    with database.session() as session:
        session.add_all(
            Asset(
                asset_type="paper",
                title=f"Selected {index:03d}",
                venue="  CvPr  ",
                year=2026,
                domain_slugs=["alpha"],
                attributes={"first_public_date": "2026-06-12"},
            )
            for index in range(65)
        )
        session.add_all(
            [
                Asset(asset_type="paper", title="Other venue", venue="CVPR Workshops", year=2026),
                Asset(asset_type="paper", title="Other year", venue="CVPR", year=2025),
                Asset(asset_type="paper", title="Missing fields"),
            ]
        )
    params = {
        "venue": " cvpr ",
        "year": 2026,
        "group_by": group_by,
        "include_facets": include_facets,
        "year_from": 2025,
        "year_to": 2027,
        "date_from": "2026-01-01",
        "date_to": "2026-12-31",
    }
    with TestClient(create_app(settings, database=database)) as client:
        ids = []
        while True:
            response = client.get("/api/v1/search", params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 65
            if include_facets:
                assert body["facets"]["years"] == {"2026": 65}
            ids.extend(
                item["unit_id"] if group_by == "paper" else item["id"] for item in body["items"]
            )
            if not body["next_cursor"]:
                break
            params["cursor"] = body["next_cursor"]
        assert len(ids) == len(set(ids)) == 65
        for impossible in [
            {"venue": "CVPRW", "year": 2026},
            {"venue": "CVPR", "year": 2026, "year_to": 2025},
            {"venue": "CVPR", "year": 2026, "date_to": "2025-12-31"},
        ]:
            result = client.get(
                "/api/v1/search", params={**impossible, "group_by": group_by}
            ).json()
            assert result["total"] == 0
            assert result["items"] == []
    engine.dispose()


@pytest.mark.parametrize(
    "filters",
    [
        {"venue": ""},
        {"venue": "   "},
        {"venue": "x" * 201},
        {"year": 1449},
        {"year": 2201},
        {"year": "2026.5"},
        {"year_from": 2026, "year_to": 2025},
        {"collection_id": "not-a-uuid"},
    ],
)
def test_search_field_validation(tmp_path, filters):
    settings, engine, database = runtime(tmp_path)
    with TestClient(create_app(settings, database=database)) as client:
        assert client.get("/api/v1/search", params=filters).status_code == 422
    engine.dispose()


def test_collection_catalog_excludes_hidden_and_deleted_members(tmp_path):
    settings, engine, database = runtime(tmp_path)
    with database.session() as session:
        collection = Asset(asset_type="collection", title="My collection")
        hidden = Asset(
            asset_type="collection", title="Domain overview", attributes={"system_hidden": True}
        )
        removed = Asset(asset_type="collection", title="Removed", deleted_at=datetime.now(UTC))
        papers = [
            Asset(asset_type="paper", title=f"Member {index:02d}", venue="ICLR", year=2026)
            for index in range(35)
        ]
        deleted = Asset(asset_type="paper", title="Deleted member", deleted_at=datetime.now(UTC))
        concealed = Asset(
            asset_type="paper", title="Concealed member", attributes={"system_hidden": "TRUE"}
        )
        unlinked = Asset(asset_type="paper", title="Removed membership")
        session.add_all([collection, hidden, removed, deleted, concealed, unlinked, *papers])
        session.flush()
        session.add_all(
            Relation(
                subject_asset_id=asset.id,
                object_asset_id=collection.id,
                relation_type="belongs_to_collection",
                source_key="manual",
                deleted_at=datetime.now(UTC) if asset is unlinked else None,
            )
            for asset in [*papers, deleted, concealed, unlinked]
        )
        cid, hidden_id, removed_id, paper_id = map(
            str, [collection.id, hidden.id, removed.id, papers[0].id]
        )
    with TestClient(create_app(settings, database=database)) as client:
        listed = client.get("/api/v1/collections").json()
        assert listed["total"] == 1
        assert listed["items"][0]["id"] == cid
        assert listed["items"][0]["member_count"] == 35
        assert client.get(f"/api/v1/collections/{cid}").status_code == 200
        for invalid in [hidden_id, removed_id, str(uuid4()), paper_id]:
            assert (
                client.get("/api/v1/search", params={"collection_id": invalid}).status_code == 404
            )
        for group_by in ["none", "paper"]:
            params = {"collection_id": cid, "venue": "iclr", "year": 2026, "group_by": group_by}
            first = client.get("/api/v1/search", params=params).json()
            assert first["total"] == 35
            assert len(first["items"]) == 30
            second = client.get(
                "/api/v1/search", params={**params, "cursor": first["next_cursor"]}
            ).json()
            assert second["total"] == 35
            assert len(second["items"]) == 5
        members = client.get(f"/api/v1/collections/{cid}/members").json()
        assert members["total"] == 35
        assert len(members["items"]) == 30
        saved = client.post(
            "/api/v1/saved-searches",
            json={
                "name": "Conference reading",
                "definition": {"venue": " ICLR ", "year": 2026, "collection_id": cid},
            },
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["definition"]["venue"] == "ICLR"
        assert saved.json()["definition"]["year"] == 2026
        for invalid in [{"venue": " "}, {"year": 2201}]:
            assert (
                client.post(
                    "/api/v1/saved-searches", json={"name": "Invalid", "definition": invalid}
                ).status_code
                == 422
            )
    engine.dispose()


def test_exact_fields_qualify_paper_before_covering_repository_hits(tmp_path, monkeypatch):
    settings, engine, database = runtime(tmp_path)

    class OfflineSearch:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("use SQL metadata matches")

    monkeypatch.setattr(
        "apps.api.routes_v1._search_runtime",
        lambda _request: SimpleNamespace(service=OfflineSearch()),
    )
    with database.session() as session:
        paper = Asset(asset_type="paper", title="Selected paper", venue="CVPR", year=2026)
        other = Asset(asset_type="paper", title="Filtered paper", venue="ICLR", year=2026)
        repository = Asset(asset_type="repository", title="Needle selected code")
        independent = Asset(
            asset_type="repository", title="Needle independent code", venue="CVPR", year=2026
        )
        session.add_all([paper, other, repository, independent])
        session.flush()
        session.add_all(
            Relation(
                subject_asset_id=parent.id,
                object_asset_id=repo.id,
                relation_type="implemented_by",
                confirmation_status="confirmed",
            )
            for parent, repo in [(paper, repository), (other, independent)]
        )
        paper_id, independent_id = str(paper.id), str(independent.id)
    with TestClient(create_app(settings, database=database)) as client:
        filters = {"q": "Needle", "group_by": "paper", "venue": "cvpr", "year": 2026}
        result = client.get("/api/v1/search", params=filters)
        assert result.status_code == 200, result.text
        assert result.json()["total"] == 2
        assert {item["unit_id"] for item in result.json()["items"]} == {paper_id, independent_id}
        only_paper = client.get("/api/v1/search", params={**filters, "type": "paper"}).json()
        assert only_paper["total"] == 1
        assert only_paper["items"][0]["unit_id"] == paper_id
    engine.dispose()


def test_dashboard_resumes_by_reading_time_not_asset_metadata_update(tmp_path):
    settings, engine, database = runtime(tmp_path)
    with database.session() as session:
        metadata_recent = Asset(
            asset_type="paper", title="Metadata recent", updated_at=datetime(2026, 3, 1, tzinfo=UTC)
        )
        read_recent = Asset(
            asset_type="paper", title="Read recent", updated_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        status_only = Asset(asset_type="paper", title="Status only")
        completed = Asset(asset_type="paper", title="Completed")
        session.add_all([metadata_recent, read_recent, status_only, completed])
        session.flush()
        for asset in [metadata_recent, read_recent, status_only, completed]:
            session.add(
                ReadingState(
                    asset_id=asset.id,
                    user_id="local",
                    status="read" if asset is completed else "reading",
                    updated_at=datetime(2026, 1, 15, tzinfo=UTC),
                )
            )
        for asset, month in [(metadata_recent, 1), (read_recent, 2), (completed, 4)]:
            pdf = Representation(
                asset_id=asset.id,
                representation_type="pdf",
                role="original",
                path_ref="paper.pdf",
                sha256="a" * 64,
                availability="available",
            )
            session.add(pdf)
            session.flush()
            session.add(
                ReadingPosition(
                    asset_id=asset.id,
                    representation_id=pdf.id,
                    page=7,
                    source_hash="a" * 64,
                    updated_at=datetime(2026, month, 1, tzinfo=UTC),
                )
            )
        recent_id = str(read_recent.id)
    with TestClient(create_app(settings, database=database)) as client:
        result = client.get("/api/v1/dashboard")
        assert result.status_code == 200, result.text
        cards = result.json()["continue_reading"]
        assert [item["title"] for item in cards] == [
            "Read recent",
            "Status only",
            "Metadata recent",
        ]
        assert (
            cards[0]["reading_position"]
            == client.get(f"/api/v1/assets/{recent_id}/reading-position").json()
        )
        assert cards[0]["reading_position"]["position"]["page"] == 7
        assert cards[0]["reading_position"]["stale"] is False
        assert cards[1]["reading_position"] == {"position": None, "stale": False}
    engine.dispose()


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
                    venue="CVPR",
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
            response = client.get(
                "/api/v1/search",
                params={"domain": "alpha", "group_by": "paper", "venue": "cvpr", "year": 2025},
            )
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
