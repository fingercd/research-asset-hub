from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.domain_views import annotate_presentation_domains, presentation_domains
from packages.domain.models import Asset, Base, Source, SourceRecord
from packages.domain.platform_models import SourceRecordAsset


def asset(domains: list[str], title: str, *, category: str = "", tags: list[str] | None = None):
    return SimpleNamespace(
        domain_slugs=domains,
        title=title,
        attributes={"category": category},
        tags=tags or [],
    )


def test_visual_domains_are_separated_by_paper_topic() -> None:
    video = asset(["generation"], "Video Diffusion Models", category="video_core")
    assert presentation_domains(video) == [
        "video-generation"
    ]
    assert presentation_domains(asset(["generation"], "Imagen", category="image_core")) == [
        "image-editing"
    ]
    video_survey = asset(["generation"], "A Survey on Video Diffusion Models", category="survey")
    assert presentation_domains(video_survey) == [
        "video-generation"
    ]
    reassigned_video = asset(["generation"], "Deep Forcing", tags=["video-deleted-review"])
    assert presentation_domains(reassigned_video) == [
        "video-generation"
    ]


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
