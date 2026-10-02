"""Derivative reads must remain tied to available, current and correctly owned evidence."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from apps.api.routes import knowledge_derivatives as routes
from apps.asset_gateway.registry import SourceConfigurationError, SourceRegistry
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Representation,
    Source,
    SourceRecord,
    SourceRetirement,
)
from packages.domain.platform_models import SourceRecordAsset
from packages.domain.serving_scope import RetiredRepresentationError
from packages.knowledge.derived import MAX_IMAGE_BYTES, DerivativeStore
from packages.knowledge.service import KnowledgeError, KnowledgeService


@pytest.fixture
def derivative_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    source_root = tmp_path / "source"
    source_root.mkdir()
    pdf = source_root / "paper.pdf"
    pdf.write_bytes(b"frozen paper source")
    code = source_root / "repository" / "src" / "model.py"
    code.parent.mkdir(parents=True)
    code.write_text("def forward(x):\n    return x + 1\n", encoding="utf-8")
    for unsafe in [
        source_root / "private.py",
        source_root / "repository" / ".git" / "config",
        source_root / "repository" / "node_modules" / "package" / "index.js",
    ]:
        unsafe.parent.mkdir(parents=True, exist_ok=True)
        unsafe.write_bytes(code.read_bytes())
    cache = tmp_path / "derived" / "v1"
    cache.mkdir(parents=True)
    with Session(engine) as session:
        source = Source(
            alias="derivative-test",
            label="Test",
            adapter="generic_manifest",
            root_path=str(source_root),
        )
        paper = Asset(asset_type="paper", title="Source-bound paper", domain_slugs=["agent"])
        repository = Asset(asset_type="repository", title="Paper code", domain_slugs=["agent"])
        session.add_all([source, paper, repository])
        session.flush()
        pdf_rep = Representation(
            asset_id=paper.id,
            source_id=source.id,
            representation_type="pdf",
            role="original",
            root_key="source",
            path_ref="paper.pdf",
            availability="available",
            sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        )
        code_rep = Representation(
            asset_id=repository.id,
            source_id=source.id,
            representation_type="repository",
            role="official",
            root_key="source",
            path_ref="repository",
            availability="available",
            materialization_kind="directory",
        )
        session.add_all([pdf_rep, code_rep])
        session.commit()
        registry = SourceRegistry({("derivative-test", "source"): source_root})
        service = KnowledgeService(session, registry, cache)

        @contextmanager
        def mock_service(request: Request):
            yield service

        monkeypatch.setattr(routes, "_service", mock_service)
        yield {
            "service": service,
            "paper": paper,
            "repository": repository,
            "pdf_rep": pdf_rep,
            "code_rep": code_rep,
            "source": source,
            "pdf": pdf,
            "code": code,
            "cache": cache,
        }
    engine.dispose()


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/"})


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _card(library: dict[str, Any], *, status: str = "reviewed") -> dict[str, Any]:
    return {
        "id": "code-pattern",
        "kind": "code",
        "domains": ["agent"],
        "quality_status": status,
        "human_reviewed": False,
        "experiment_reproduced": False,
        "asset_ids": [str(library["paper"].id)],
        "evidence": [
            {
                "asset_id": str(library["repository"].id),
                "representation_id": str(library["code_rep"].id),
                "path": "src/model.py",
                "source_hash": hashlib.sha256(library["code"].read_bytes()).hexdigest(),
            }
        ],
    }


def _methodology(library: dict[str, Any], cards: list[dict[str, Any]], *, candidates=False):
    _write_json(
        library["cache"].parent / "methodology.json",
        {
            "schema_version": 1,
            "cards": cards,
        },
    )
    return routes.get_methodology(
        _request(),
        kind="code",
        domain="agent",
        q="",
        limit=20,
        include_candidates=candidates,
    )


def _retire(library: dict[str, Any]) -> None:
    session = library["service"].session
    backup = BackupRun(destination_label="derivative-test")
    session.add(backup)
    session.flush()
    session.add(
        SourceRetirement(
            source_id=library["source"].id,
            backup_id=backup.id,
            payload={},
            payload_digest="a" * 64,
            frozen_manifest_sha256="a" * 64,
            frozen_source_config_sha256="a" * 64,
            signature_algorithm="test",
            signer_key_id="test",
            signature="test",
            reason="test",
            record_set_digest="a" * 64,
            representation_set_digest="a" * 64,
            mount_set_digest="a" * 64,
            collection_digest="a" * 64,
            applied_by="test",
        )
    )
    session.flush()


def _figures(
    library: dict[str, Any],
    *,
    owner: Asset | None = None,
    digest: str | None = None,
) -> dict[str, Any]:
    owner = owner if owner is not None else library["paper"]
    identifier = f"{owner.id}-fig-1"
    folder = library["cache"] / str(owner.id)
    figure = {
        "figure_id": identifier,
        "page": 1,
        "figure_number": "1",
        "caption": "Test source diagram",
        "preview_path": "figures/preview.png",
        "image_path": "figures/original.png",
        "quality_status": "pending_review",
        "human_reviewed": False,
    }
    _write_json(
        folder / "figures.json",
        {
            "schema_version": 1,
            "asset_id": str(owner.id),
            "representation_id": str(library["pdf_rep"].id),
            "source_hash": digest if digest is not None else library["pdf_rep"].sha256,
            "figures": [figure],
        },
    )
    _write_json(
        folder / "metadata.json",
        {
            "schema_version": 1,
            "title": owner.title,
            "domains": ["agent"],
        },
    )
    image_path = folder / figure["preview_path"]
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    return {
        **figure,
        "asset_id": str(owner.id),
        "representation_id": str(library["pdf_rep"].id),
        "source_hash": digest if digest is not None else library["pdf_rep"].sha256,
    }


def test_code_methodology_rechecks_frozen_file_hash_and_does_not_claim_human_review(
    derivative_library,
):
    library = derivative_library
    card = _card(library)
    before = _methodology(library, [card])
    assert before["items"] == [card]
    assert before["items"][0]["human_reviewed"] is False
    assert before["items"][0]["experiment_reproduced"] is False
    assert "distinct from human review" in before["review_scope"]
    library["code"].write_text("def forward(x):\n    return x + 2\n", encoding="utf-8")
    after = _methodology(library, [card])
    assert after["items"] == []
    assert after["excluded_stale_or_unavailable"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "disabled",
        "retired",
        "representation_deleted",
        "asset_hidden",
        "wrong_owner",
        "traversal",
        "hidden_component",
        "dependency_component",
    ],
)
def test_methodology_never_serves_unavailable_or_unsafe_code_evidence(
    derivative_library,
    failure,
):
    library = derivative_library
    card = _card(library)
    if failure == "missing":
        library["code"].unlink()
    elif failure == "disabled":
        library["source"].enabled = False
    elif failure == "retired":
        _retire(library)
    elif failure == "representation_deleted":
        library["code_rep"].deleted_at = datetime.now(UTC)
    elif failure == "asset_hidden":
        library["repository"].attributes = {"system_hidden": True}
    elif failure == "wrong_owner":
        card["evidence"][0]["asset_id"] = str(library["paper"].id)
    else:
        card["evidence"][0]["path"] = {
            "traversal": "../private.py",
            "hidden_component": ".git/config",
            "dependency_component": "node_modules/package/index.js",
        }[failure]
    result = _methodology(library, [card])
    assert result["items"] == []
    assert result["excluded_stale_or_unavailable"] == 1


@pytest.mark.parametrize("empty", ["evidence", "asset_ids"])
def test_reviewed_methodology_requires_source_bound_support(derivative_library, empty):
    library = derivative_library
    card = _card(library)
    card[empty] = []
    result = _methodology(library, [card])
    assert result["items"] == []
    assert result["excluded_stale_or_unavailable"] == 1


def test_methodology_candidate_visibility_requires_opt_in_and_keeps_review_status(
    derivative_library,
):
    library = derivative_library
    card = _card(library, status="candidate")
    assert _methodology(library, [card])["items"] == []
    assert _methodology(library, [card], candidates=True)["items"] == [card]


@pytest.mark.parametrize("failure", ["missing", "changed", "disabled", "retired", "wrong_owner"])
def test_figure_read_requires_live_owned_original_source(derivative_library, failure):
    library = derivative_library
    figure = _figures(library)
    if failure == "missing":
        library["pdf"].unlink()
    elif failure == "changed":
        library["pdf"].write_bytes(b"different paper version")
    elif failure == "disabled":
        library["source"].enabled = False
    elif failure == "retired":
        _retire(library)
    else:
        library["pdf_rep"].asset_id = library["repository"].id
    with pytest.raises((KnowledgeError, RetiredRepresentationError)):
        routes.get_figure(figure["figure_id"], _request())
    with pytest.raises((KnowledgeError, RetiredRepresentationError)):
        routes.get_figure_image(figure["figure_id"], _request())


def test_figure_metadata_and_png_preserve_pending_review(derivative_library):
    library = derivative_library
    figure = _figures(library)
    metadata = routes.get_figure(figure["figure_id"], _request())
    assert metadata["quality_status"] == "pending_review"
    assert metadata["human_reviewed"] is False
    image = routes.get_figure_image(figure["figure_id"], _request())
    assert image.media_type == "image/png"
    assert image.body.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.headers["cache-control"] == "private, no-store"


def test_figure_search_does_not_reuse_verification_for_foreign_owner(derivative_library):
    library = derivative_library
    _figures(library)
    other = Asset(asset_type="paper", title="Forged derivative owner", domain_slugs=["agent"])
    library["service"].session.add(other)
    library["service"].session.flush()
    _figures(library, owner=other)
    result = routes.search_derivatives(_request(), "diagram", "agent", "figure", "original", 20)
    assert result["total"] == 1
    assert result["items"][0]["asset_id"] == str(library["paper"].id)
    assert result["excluded_stale_or_unavailable"] == 1


def test_analysis_pdf_is_not_reported_as_original_figure(derivative_library):
    library = derivative_library
    _figures(library)
    library["pdf_rep"].role = "analysis"
    result = routes.search_derivatives(_request(), "diagram", "agent", "figure", "original", 20)
    assert result["items"] == []


@pytest.mark.parametrize("path", ["../private.png", "figures/../../private.png"])
def test_figure_preview_cannot_escape_paper_derivative_root(derivative_library, path):
    library = derivative_library
    figure = _figures(library)
    private = library["cache"] / "private.png"
    private.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    figure["preview_path"] = path
    with pytest.raises(KnowledgeError):
        DerivativeStore(library["cache"]).image(figure)


@pytest.mark.parametrize("payload", [b"<svg>active</svg>", b"<html>unexpected</html>", b"bad"])
def test_figure_preview_rejects_non_png_jpeg(derivative_library, payload):
    library = derivative_library
    figure = _figures(library)
    (library["cache"] / str(library["paper"].id) / figure["preview_path"]).write_bytes(payload)
    with pytest.raises(KnowledgeError) as error:
        DerivativeStore(library["cache"]).image(figure)
    assert error.value.code == "invalid_image"


def test_figure_preview_size_limit_is_enforced(derivative_library):
    library = derivative_library
    figure = _figures(library)
    image = library["cache"] / str(library["paper"].id) / figure["preview_path"]
    with image.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(KnowledgeError) as error:
        DerivativeStore(library["cache"]).image(figure)
    assert error.value.code == "image_too_large"


@pytest.mark.parametrize(
    "field, value",
    [
        ("asset_id", "not-a-uuid"),
        ("representation_id", "not-a-uuid"),
        ("figures", "not-a-list"),
    ],
)
def test_corrupt_figure_manifest_returns_explicit_derivative_error(
    derivative_library,
    field,
    value,
):
    library = derivative_library
    _figures(library)
    path = library["cache"] / str(library["paper"].id) / "figures.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = value
    _write_json(path, document)
    with pytest.raises(KnowledgeError) as error:
        DerivativeStore(library["cache"]).list_figures()
    assert error.value.code == "invalid_derivative"


def test_metadata_symlink_outside_derivative_root_is_rejected(derivative_library):
    library = derivative_library
    _figures(library)
    private = library["cache"].parent.parent / "private.json"
    _write_json(private, {"schema_version": 1, "title": "private text", "domains": ["agent"]})
    metadata = library["cache"] / str(library["paper"].id) / "metadata.json"
    metadata.unlink()
    try:
        metadata.symlink_to(private)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks.")
    with pytest.raises(KnowledgeError) as error:
        DerivativeStore(library["cache"]).list_figures()
    assert error.value.code == "invalid_derivative"


@pytest.mark.parametrize("corruption", ["invalid_uuid", "missing_hash"])
def test_bad_methodology_evidence_is_an_explicit_failure(derivative_library, corruption):
    library = derivative_library
    card = _card(library)
    if corruption == "invalid_uuid":
        card["evidence"][0]["representation_id"] = "invalid-uuid"
    else:
        card["evidence"][0].pop("source_hash")
    try:
        result = _methodology(library, [card])
    except KnowledgeError:
        return
    assert result["items"] == []
    assert result["excluded_stale_or_unavailable"] == 1


def _taxonomy(library):
    path = library["cache"].parent / "taxonomy.yaml"
    path.write_text(
        "domains:\n  - id: agent\n    label: 智能体\n    aliases: [agents]\n"
        "  - id: generation\n    label: 图像与视频生成\n"
        "  - id: video-generation\n    label: 视频生成\n"
        "  - id: image-editing\n    label: 图像生成与编辑\n"
        "  - id: embodied-ai-compression\n    label: 具身智能与导航压缩\n"
        "  - id: navigation-compression\n    label: 导航压缩\n",
        encoding="utf-8",
    )
    library["service"].taxonomy_path = path


@pytest.mark.parametrize("domain", ["视频生成", "video-generation"])
def test_figure_search_filters_current_presentation_domain_instead_of_cache_metadata(
    derivative_library, domain
):
    library = derivative_library
    _taxonomy(library)
    _figures(library)
    library["paper"].domain_slugs = ["generation"]
    library["paper"].attributes = {"category": "video_core"}
    result = routes.search_derivatives(_request(), "diagram", domain, "figure", "original", 20)
    assert result["total"] == 1
    assert result["domain"] == "video-generation"
    assert result["items"][0]["domains"] == [
        "agent"
    ]  # Frozen derivative classification remains traceable.
    other = routes.search_derivatives(
        _request(), "diagram", "image-editing", "figure", "original", 20
    )
    assert other["total"] == 0


def test_methodology_chinese_alias_and_navigation_mapping_use_live_asset_domains(
    derivative_library,
):
    library = derivative_library
    _taxonomy(library)
    card = _card(library)
    library["paper"].domain_slugs = ["navigation-compression"]
    _write_json(
        library["cache"].parent / "methodology.json", {"schema_version": 1, "cards": [card]}
    )
    result = routes.get_methodology(
        _request(), kind="code", domain="具身智能与导航压缩", q="", limit=20
    )
    assert result["items"] == [card]
    assert result["domain"] == "embodied-ai-compression"
    assert (
        routes.get_methodology(_request(), kind="code", domain="智能体", q="", limit=20)["items"]
        == []
    )


def test_methodology_generation_repository_inherits_source_paper_domain(derivative_library):
    library = derivative_library
    _taxonomy(library)
    session = library["service"].session
    library["paper"].domain_slugs = ["generation"]
    library["paper"].attributes = {"category": "video_core"}
    library["repository"].domain_slugs = ["generation"]
    record = SourceRecord(
        source_id=library["source"].id, source_record_id="video", asset_id=library["paper"].id
    )
    session.add(record)
    session.flush()
    session.add_all(
        [
            SourceRecordAsset(source_record_id=record.id, asset_id=library["paper"].id),
            SourceRecordAsset(
                source_record_id=record.id, asset_id=library["repository"].id, role="repository"
            ),
        ]
    )
    session.flush()
    card = _card(library)
    card["asset_ids"] = [str(library["repository"].id)]
    _write_json(
        library["cache"].parent / "methodology.json", {"schema_version": 1, "cards": [card]}
    )
    assert routes.get_methodology(_request(), kind="code", domain="视频生成", q="", limit=20)[
        "items"
    ] == [card]
    assert (
        routes.get_methodology(_request(), kind="code", domain="图像生成与编辑", q="", limit=20)[
            "items"
        ]
        == []
    )


def test_derivative_keywords_expand_only_literal_aliases_and_conditions_stay_unverified(
    derivative_library,
):
    library = derivative_library
    card = {**_card(library), "title": "Quantization code pattern"}
    _write_json(
        library["cache"].parent / "methodology.json", {"schema_version": 1, "cards": [card]}
    )
    result = routes.search_derivatives(
        _request(),
        "我需要一种合适的方法",
        "agent",
        "code_pattern",
        "analysis",
        20,
        keywords=["量化"],
        constraints=["不重新训练"],
    )
    assert result["total"] == 1
    assert result["query_plan"]["mode"] == "literal_keywords"
    assert result["query_plan"]["needs_evidence_verification"] is True
    assert result["items"][0]["ranking"]["conditions"][0]["needs_evidence_verification"] is True
    assert result["items"][0]["human_reviewed"] is False
    figure = _figures(library)
    path = library["cache"] / str(library["paper"].id) / "figures.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["figures"][0]["caption"] = "Quantization architecture"
    _write_json(path, manifest)
    figures = routes.search_derivatives(
        _request(), "请搜索相关结构图", "agent", "figure", "original", 20, keywords=["量化"]
    )
    assert figures["items"][0]["figure_id"] == figure["figure_id"]


@pytest.mark.parametrize(
    "asset_type,source_type",
    [
        ("figure", "original"),
        ("code_pattern", "analysis"),
        ("writing_pattern", "original"),
        ("figure", "analysis"),
    ],
)
@pytest.mark.parametrize("keywords,constraints", [(["term"] * 9, []), ([], ["condition"] * 7)])
def test_all_derivative_types_enforce_query_plan_parameter_bounds(
    derivative_library, asset_type, source_type, keywords, constraints
):
    with pytest.raises(KnowledgeError) as error:
        routes.search_derivatives(
            _request(),
            "",
            None,
            asset_type,
            source_type,
            20,
            keywords=keywords,
            constraints=constraints,
        )
    assert error.value.status == 422


def test_source_configuration_error_is_not_counted_as_stale_methodology(
    derivative_library, monkeypatch
):
    library = derivative_library
    card = _card(library)

    def invalid_source(*args, **kwargs):
        raise SourceConfigurationError("Invalid configuration")

    monkeypatch.setattr(routes, "local_code_repository", invalid_source)
    with pytest.raises(SourceConfigurationError):
        _methodology(library, [card])


def test_bad_source_configuration_propagates_from_figure_verification(
    derivative_library, monkeypatch
):
    library = derivative_library
    _figures(library)

    def invalid_source(*args, **kwargs):
        raise SourceConfigurationError("Invalid configuration")

    monkeypatch.setattr(library["service"], "require_representation", invalid_source)
    with pytest.raises(SourceConfigurationError):
        routes.search_derivatives(_request(), "diagram", "agent", "figure", "original", 20)


def test_figure_details_expose_only_same_page_warning_and_agent_review_fields(derivative_library):
    library = derivative_library
    figure = _figures(library)
    path = library["cache"] / str(library["paper"].id) / "figures.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["warnings"] = [
        {
            "code": "source_clipped",
            "page_index": 0,
            "message": "Source page edge clips the figure",
            "internal_path": "private",
            "agent_review": {
                "review_id": "review-1",
                "human_reviewed": False,
                "issue_category": "source_page_clipped",
                "observation": "Bottom axis is clipped",
                "confidence": "high",
                "internal_path": "private",
            },
        },
        {"code": "other_page_warning", "page_index": 1, "message": "Other page"},
    ]
    _write_json(path, manifest)
    result = routes.get_figure(figure["figure_id"], _request())
    assert len(result["page_warnings"]) == 1
    warning = result["page_warnings"][0]
    assert warning["code"] == "source_clipped"
    assert warning["agent_review"]["human_reviewed"] is False
    assert "internal_path" not in json.dumps(warning)
    assert result["quality_status"] == "pending_review"
    assert result["human_reviewed"] is False


@pytest.mark.parametrize(
    "parameter,terms",
    [
        ("keywords", ["term"] * 9),
        ("constraints", ["condition"] * 7),
        ("keywords", ["x" * 81]),
        ("constraints", ["x" * 161]),
    ],
)
def test_methodology_http_validates_list_and_term_limits(derivative_library, parameter, terms):
    app = FastAPI()
    app.add_api_route("/methodology", routes.get_methodology, methods=["GET"])
    with TestClient(app) as client:
        response = client.get("/methodology", params=[(parameter, term) for term in terms])
    assert response.status_code == 422


def test_unknown_domain_is_an_explicit_query_error(derivative_library):
    library = derivative_library
    _taxonomy(library)
    with pytest.raises(KnowledgeError) as error:
        routes.get_methodology(_request(), kind="code", domain="不存在的领域", q="", limit=20)
    assert error.value.status == 422
    assert error.value.code == "invalid_query"
