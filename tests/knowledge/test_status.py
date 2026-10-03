from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import (
    Activity,
    Asset,
    BackupRun,
    Base,
    DocumentChunk,
    FieldResolution,
    IndexState,
    Job,
    Representation,
    Source,
    SourceRecord,
    SourceRetirement,
)
from packages.domain.platform_models import SourceRecordAsset
from packages.knowledge.status import build_knowledge_status


@pytest.fixture
def status_library(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        "domains:\n"
        "  - id: ai-for-math\n    label: 数学人工智能\n    aliases: [AI for Math]\n"
        "  - id: image-editing\n    label: 图像生成与编辑\n"
        "  - id: video-generation\n    label: 视频生成\n"
        "  - id: embodied-ai-compression\n    label: 具身智能与导航压缩\n"
        "  - id: generation\n    hidden_from_catalog: true\n"
        "  - id: navigation-compression\n    hidden_from_catalog: true\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        taxonomy_config_path=taxonomy,
        sources_config_path=tmp_path / "sources.yaml",
        search_code_index_enabled=False,
    )
    with Session(engine) as session:
        source = Source(
            alias="math",
            label="数学来源",
            adapter="generic_manifest",
            root_path=str(tmp_path),
            config={"domain_slugs": ["ai-for-math"], "token": "never return me"},
        )
        disabled = Source(
            alias="disabled",
            label="Disabled",
            adapter="generic_manifest",
            root_path=str(tmp_path),
            enabled=False,
        )
        paper = Asset(
            asset_type="paper", title="Mathematical Reasoning", domain_slugs=["ai-for-math"]
        )
        image = Asset(
            asset_type="paper",
            title="Image diffusion",
            domain_slugs=["generation"],
            attributes={"category": "image_core"},
        )
        video = Asset(
            asset_type="paper",
            title="Video diffusion",
            domain_slugs=["generation"],
            attributes={"category": "video_core"},
        )
        repository = Asset(
            asset_type="repository", title="Generic repository", domain_slugs=["generation"]
        )
        navigation = Asset(
            asset_type="paper", title="Nav compression", domain_slugs=["navigation-compression"]
        )
        hidden = Asset(
            asset_type="paper",
            title="Hidden",
            domain_slugs=["ai-for-math"],
            attributes={"system_hidden": True},
        )
        session.add_all([source, disabled, paper, image, video, repository, navigation, hidden])
        session.flush()
        record = SourceRecord(
            source_id=source.id, source_record_id="video-paper", asset_id=video.id
        )
        session.add(record)
        session.flush()
        session.add_all(
            [
                SourceRecordAsset(source_record_id=record.id, asset_id=video.id),
                SourceRecordAsset(
                    source_record_id=record.id, asset_id=repository.id, role="repository"
                ),
            ]
        )
        pdf = Representation(
            asset_id=paper.id,
            source_id=source.id,
            representation_type="pdf",
            role="original",
            path_ref="missing-paper.pdf",
            availability="available",
        )
        remote = Representation(
            asset_id=paper.id,
            source_id=source.id,
            representation_type="html",
            role="guide",
            external_url="https://example.org/analysis",
            availability="available",
        )
        missing = Representation(
            asset_id=paper.id,
            source_id=source.id,
            representation_type="markdown",
            role="guide",
            path_ref="missing.md",
            availability="missing",
        )
        blocked = Representation(
            asset_id=paper.id,
            source_id=disabled.id,
            representation_type="html",
            role="original",
            path_ref="blocked.html",
            availability="available",
        )
        session.add_all([pdf, remote, missing, blocked])
        session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                Activity(
                    asset_id=paper.id,
                    representation_id=pdf.id,
                    activity_type="extraction",
                    tool_name="parser",
                    status="succeeded",
                    result={"status": "succeeded"},
                    started_at=now - timedelta(days=1),
                ),
                Activity(
                    asset_id=paper.id,
                    representation_id=pdf.id,
                    activity_type="extraction",
                    tool_name="parser",
                    status="succeeded",
                    result={"status": "partial"},
                    started_at=now,
                ),
                Activity(
                    asset_id=paper.id,
                    representation_id=remote.id,
                    activity_type="extraction",
                    tool_name="parser",
                    status="failed",
                    result={"status": "failed"},
                    started_at=now,
                ),
            ]
        )
        chunk = DocumentChunk(
            asset_id=paper.id,
            representation_id=pdf.id,
            chunk_index=0,
            chunk_kind="pdf",
            text_content="BODY SHOULD NEVER BE LOADED",
            content_hash="a" * 64,
            parser_name="parser",
            parser_version="1",
        )
        session.add(chunk)
        session.flush()
        session.add_all(
            [
                Job(
                    job_type="extract_representation",
                    status="pending",
                    parameters={"representation_id": str(pdf.id)},
                ),
                Job(
                    job_type="index_asset", status="running", parameters={"asset_id": str(paper.id)}
                ),
                Job(
                    job_type="index_asset", status="failed", parameters={"asset_id": str(video.id)}
                ),
                IndexState(
                    index_name="assets",
                    index_kind="keyword",
                    entity_type="asset",
                    entity_id=paper.id,
                    content_digest="a" * 64,
                    version="v1",
                    status="ready",
                ),
                IndexState(
                    index_name="assets",
                    index_kind="keyword",
                    entity_type="asset",
                    entity_id=paper.id,
                    content_digest="b" * 64,
                    version="v1",
                    status="ready",
                ),
                IndexState(
                    index_name="documents",
                    index_kind="keyword",
                    entity_type="chunk",
                    entity_id=chunk.id,
                    content_digest="c" * 64,
                    version="v1",
                    status="ready",
                ),
                IndexState(
                    index_name="documents",
                    index_kind="keyword",
                    entity_type="asset",
                    entity_id=paper.id,
                    content_digest="d" * 64,
                    version="1",
                    status="ready",
                ),
            ]
        )
        session.commit()
        registry = SourceRegistry(
            {
                ("math", "source"): tmp_path,
                ("new-config-only", "source"): tmp_path,
                ("disabled", "source"): tmp_path,
                ("math", "historical-pdf"): tmp_path / "removed-historical-root",
            }
        )
        yield session, registry, tmp_path / "cache", settings, engine
    engine.dispose()


def test_status_reports_actual_domains_partial_extraction_and_separate_index_versions(
    status_library,
):
    session, registry, cache_root, settings, _ = status_library
    result = build_knowledge_status(session, registry, cache_root, settings, domain="ai-for-math")
    assert result["read_only"] and result["schema_version"] == 2
    assert result["counts"] == {"assets": 1, "papers": 1, "repositories": 0, "representations": 4}
    domains = {item["slug"]: item for item in result["domains"]}
    assert domains["ai-for-math"]["label"] == "数学人工智能"
    assert domains["ai-for-math"]["aliases"] == ["AI for Math"]
    assert domains["image-editing"]["paper_count"] == 1
    assert domains["video-generation"]["paper_count"] == 1
    assert domains["video-generation"]["repository_count"] == 1
    assert domains["embodied-ai-compression"]["paper_count"] == 1
    assert "generation" not in domains and "navigation-compression" not in domains
    assert {tuple(row.values()) for row in result["extraction"]["latest_activity_counts"]} == {
        ("failed", "failed", 1),
        ("succeeded", "partial", 1),
    }
    assert result["extraction"]["without_activity_count"] == 1
    assert result["document_chunks"] == 1
    assert result["jobs"]["counts"] == {"pending": 1, "running": 1, "failed": 0}
    states = result["keyword_index"]["states"]
    asset_index = next(item for item in states if item["index_name"] == "assets")
    assert asset_index["state_counts"]["ready"] == 2
    assert asset_index["distinct_entities_by_state"]["ready"] == 1
    assert {item["version_role"] for item in states} == {"formal", "legacy"}
    assert result["keyword_index"]["remote_service_verified"] is False
    assert all(item["serving_enabled"] is (item["index_name"] == "assets") for item in states)
    assert result["representations"]["source_files_verified"] is False
    assert result["representations"]["verification"] == "ledger_and_root_registration_only"
    assert result["representations"]["representations_with_registered_root"] == 1
    assert result["source_roots"] == {
        "configured": 4,
        "verification": "registration_only",
        "source_files_verified": False,
    }
    assert result["representations"]["unavailable_or_unverified_reasons"] == {
        "remote_location_not_verified": 1,
        "representation_missing": 1,
        "source_disabled_or_absent": 1,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(settings.taxonomy_config_path.parent) not in serialized
    assert "never return me" not in serialized
    assert result["configured_sources_not_in_database"] == ["new-config-only"]
    assert not session.dirty and not session.new


def test_status_uses_bounded_queries_and_never_selects_chunk_bodies(status_library, monkeypatch):
    session, registry, cache_root, settings, engine = status_library
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda connection, cursor, statement, parameters, context, many: statements.append(
            statement
        ),
    )
    # A missing paper.pdf is still ledger-only available: status must not probe
    # any representation path or hash it in order to make that claim.
    monkeypatch.setattr(
        registry, "resolve", lambda *args: pytest.fail("status resolved a source file")
    )
    result = build_knowledge_status(session, registry, cache_root, settings)
    assert result["counts"]["papers"] == 4
    assert result["jobs"]["counts"] == {"pending": 1, "running": 1, "failed": 1}
    assert len(statements) <= 10
    assert all("document_chunks.text_content" not in statement for statement in statements)
    assert all(
        not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )


def test_status_keeps_excluded_pdf_in_ledger_but_removes_its_active_coverage(status_library):
    session, registry, cache_root, settings, _ = status_library
    pdf = session.scalar(select(Representation).where(Representation.representation_type == "pdf"))
    session.add(
        FieldResolution(
            entity_type="representation",
            entity_id=pdf.id,
            field_path="/serving",
            resolution_kind="override",
            override_value={"excluded": True, "reason": "wrong original PDF"},
        )
    )
    session.commit()
    result = build_knowledge_status(session, registry, cache_root, settings, domain="ai-for-math")
    assert result["counts"]["representations"] == 4
    assert result["document_chunks"] == 0
    assert result["extraction"]["representations_with_chunks"] == 0
    assert result["representations"]["representations_with_registered_root"] == 0


@pytest.mark.parametrize("inactive_source", ["disabled", "retired"])
def test_index_join_and_chunk_counts_exclude_hidden_assets_and_inactive_sources(
    status_library,
    inactive_source,
):
    session, registry, cache_root, settings, _ = status_library
    hidden = session.scalar(select(Asset).where(Asset.title == "Hidden"))
    source = session.scalar(select(Source).where(Source.alias == "math"))
    disabled_rep = session.scalar(
        select(Representation).join(Source).where(Source.alias == "disabled")
    )
    if inactive_source == "retired":
        disabled_source = session.scalar(select(Source).where(Source.alias == "disabled"))
        disabled_source.enabled = True
        backup = BackupRun(destination_label="status-test")
        session.add(backup)
        session.flush()
        session.add(
            SourceRetirement(
                source_id=disabled_source.id,
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
    hidden_rep = Representation(
        asset_id=hidden.id,
        source_id=source.id,
        representation_type="pdf",
        role="original",
        path_ref="hidden.pdf",
        availability="available",
    )
    session.add(hidden_rep)
    session.flush()
    for representation in (hidden_rep, disabled_rep):
        chunk = DocumentChunk(
            asset_id=representation.asset_id,
            representation_id=representation.id,
            chunk_index=0,
            chunk_kind="pdf",
            text_content="excluded",
            content_hash="e" * 64,
            parser_name="parser",
            parser_version="1",
        )
        session.add(chunk)
        session.flush()
        session.add(
            IndexState(
                index_name="documents",
                index_kind="keyword",
                entity_type="chunk",
                entity_id=chunk.id,
                content_digest="e" * 64,
                version="v1",
                status="ready",
            )
        )
    session.commit()
    result = build_knowledge_status(session, registry, cache_root, settings, domain="ai-for-math")
    assert result["document_chunks"] == 1
    assert result["extraction"]["representations_with_chunks"] == 1
    assert result["extraction"]["assets_with_chunks"] == 1
    document_index = next(
        item for item in result["keyword_index"]["states"] if item["entity_type"] == "chunk"
    )
    assert document_index["state_counts"] == {"ready": 1}
    assert document_index["distinct_entities_by_state"] == {"ready": 1}
    assert result["keyword_index"]["index_ledger_scope"] == "active_domain_entities"
    global_result = build_knowledge_status(session, registry, cache_root, settings)
    global_document_index = next(
        item for item in global_result["keyword_index"]["states"] if item["entity_type"] == "chunk"
    )
    # The global ledger is deliberately distinct from the current serving scope.
    assert global_document_index["state_counts"] == {"ready": 3}
    assert global_document_index["distinct_entities_by_state"] == {"ready": None}
    assert global_document_index["distinct_entities_computed"] is False
    assert global_result["keyword_index"]["may_include_hidden_or_retired_entities"] is True
    assert global_result["keyword_index"]["index_ledger_scope"] == "all_index_ledger_entities"
    global_asset_index = next(
        item for item in global_result["keyword_index"]["states"] if item["index_name"] == "assets"
    )
    assert global_asset_index["state_counts"] == {"ready": 2}
    assert global_asset_index["distinct_entities_by_state"] == {"ready": 1}
