from types import SimpleNamespace

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from apps.api.domain_views import annotate_presentation_domains, presentation_domains
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Asset, Base, Source, SourceRecord
from packages.domain.platform_models import SourceRecordAsset
from packages.knowledge.service import KnowledgeService


def asset(domains: list[str], title: str, *, category: str = "", tags: list[str] | None = None):
    return SimpleNamespace(
        domain_slugs=domains,
        title=title,
        attributes={"category": category},
        tags=tags or [],
    )


def test_visual_domains_are_separated_by_paper_topic() -> None:
    video = asset(["generation"], "Video Diffusion Models", category="video_core")
    assert presentation_domains(video) == ["video-generation"]
    assert presentation_domains(asset(["generation"], "Imagen", category="image_core")) == [
        "image-editing"
    ]
    video_survey = asset(["generation"], "A Survey on Video Diffusion Models", category="survey")
    assert presentation_domains(video_survey) == ["video-generation"]
    reassigned_video = asset(["generation"], "Deep Forcing", tags=["video-deleted-review"])
    assert presentation_domains(reassigned_video) == ["video-generation"]


def test_navigation_compression_joins_embodied_compression() -> None:
    assert presentation_domains(asset(["navigation-compression"], "Navigation cache")) == [
        "embodied-ai-compression"
    ]


def test_repository_follows_its_source_paper_without_title_guessing() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            alias="generation-example", label="生成", adapter="generation", root_path="."
        )
        paper = Asset(
            asset_type="paper",
            title="Long Video Generation",
            domain_slugs=["generation"],
            attributes={"category": "video_core"},
        )
        repository = Asset(
            asset_type="repository",
            title="example/opaque-name",
            domain_slugs=["generation"],
        )
        session.add_all([source, paper, repository])
        session.flush()
        record = SourceRecord(
            source_id=source.id, source_record_id="video-core-01", asset_id=paper.id
        )
        session.add(record)
        session.flush()
        session.add_all(
            [
                SourceRecordAsset(source_record_id=record.id, asset_id=paper.id, role="primary"),
                SourceRecordAsset(
                    source_record_id=record.id, asset_id=repository.id, role="repository"
                ),
            ]
        )
        session.flush()
        annotate_presentation_domains(session, [paper, repository])
        assert presentation_domains(repository) == ["video-generation"]
    engine.dispose()


def test_repository_only_search_batches_source_paper_metadata_without_loading_body(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    topics = [
        ("Opaque paper", "video_core", [], "video-generation"),
        ("Misleading video title", "image_core", [], "image-editing"),
        ("A survey of video models", "survey", [], "video-generation"),
        ("Deep Forcing", "", ["video-deleted-review"], "video-generation"),
    ]
    expected = {}
    with Session(engine) as session:
        source = Source(
            alias="generation-test", label="Generation", adapter="generation", root_path="."
        )
        session.add(source)
        session.flush()
        for index, (title, category, tags, domain) in enumerate(topics):
            paper = Asset(
                asset_type="paper",
                title=title,
                domain_slugs=["generation"],
                attributes={"category": category},
                tags=tags,
                summary="guide body",
                content="original body",
                analysis="analysis body",
            )
            repository = Asset(
                asset_type="repository",
                title=f"Opaque repository {index}",
                domain_slugs=["generation", "secondary"],
            )
            session.add_all([paper, repository])
            session.flush()
            record = SourceRecord(
                source_id=source.id, source_record_id=str(index), asset_id=paper.id
            )
            session.add(record)
            session.flush()
            session.add_all(
                [
                    SourceRecordAsset(
                        source_record_id=record.id, asset_id=paper.id, role="primary"
                    ),
                    SourceRecordAsset(
                        source_record_id=record.id, asset_id=repository.id, role="repository"
                    ),
                ]
            )
            expected[str(repository.id)] = ["secondary", domain]
        session.commit()
    statements = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    # A new session ensures associated papers are absent from the identity map.
    with Session(engine) as session:
        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            result = KnowledgeService(session, SourceRegistry({}), tmp_path).search_assets(
                "Opaque repository", asset_type="repository"
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)
        assert result["total"] == len(topics)
        assert {item["asset_id"]: item["domains"] for item in result["items"]} == expected
        assert all(
            item["source_domains"] == ["generation", "secondary"] for item in result["items"]
        )
    assert len(statements) == 4  # candidates, source links, batched papers, identifiers
    paper_queries = [sql for sql in statements if "JOIN assets" in sql]
    assert len(paper_queries) == 1
    assert all(
        f"assets.{field}" in paper_queries[0]
        for field in ("id", "asset_type", "title", "domain_slugs", "attributes", "tags")
    )
    assert all(
        f"assets.{field}" not in sql
        for sql in statements
        for field in ("content", "summary", "analysis")
    )
    engine.dispose()
