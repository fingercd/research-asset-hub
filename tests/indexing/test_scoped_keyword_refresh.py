from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from packages.domain.models import (
    Annotation,
    Asset,
    BackupRun,
    Base,
    DocumentChunk,
    IndexState,
    Representation,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRetirement,
)
from packages.indexing import (
    IndexBuilder,
    InMemoryKeywordIndex,
    PostgresIndexCatalog,
    RemoteIndexSuite,
)
from packages.indexing.builder import KEYWORD_INDEX_NAMES, resolve_asset_scope
from packages.indexing.meili import HttpxJSONTransport, MeilisearchAdmin


class _ScopedTransport:
    def __init__(self, indexes: dict[str, InMemoryKeywordIndex]) -> None:
        self.indexes = indexes
        self.wait_for_tasks = True
        self.fail = False
        self.fetches: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        name = path.split("/")[2]
        if path.endswith("/stats"):
            return {"numberOfDocuments": len(self.indexes[name].documents)}
        assert method == "POST" and path.endswith("/documents/fetch")
        assert payload is not None
        asset_id = json.loads(str(payload["filter"]).removeprefix("asset_id = "))
        self.fetches.append((name, asset_id))
        documents = sorted(
            [item for item in self.indexes[name].documents.values() if item.asset_id == asset_id],
            key=lambda item: item.document_id,
        )
        offset, limit = int(payload["offset"]), int(payload["limit"])
        return {
            "results": [
                {"logical_document_id": item.document_id, "asset_id": item.asset_id}
                for item in documents[offset : offset + limit]
            ]
        }

    def flush_pending_tasks(self) -> None:
        if self.fail:
            raise RuntimeError("remote task failed")


class _ScopedSuite:
    def __init__(self) -> None:
        self.keyword_indexes = {name: InMemoryKeywordIndex() for name in KEYWORD_INDEX_NAMES}
        self.meili_transport = _ScopedTransport(self.keyword_indexes)
        self.meili_admin = MeilisearchAdmin(cast(HttpxJSONTransport, self.meili_transport))


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def _seed(session: Session, name: str) -> tuple[Asset, Representation, DocumentChunk]:
    source = Source(alias=name, label=name, adapter="generic", root_path=f"/{name}")
    asset = Asset(asset_type="paper", title=f"Paper {name}", abstract=f"Abstract {name}")
    session.add_all([source, asset])
    session.flush()
    record = SourceRecord(source_id=source.id, source_record_id=name, asset_id=asset.id)
    representation = Representation(
        asset_id=asset.id,
        source_id=source.id,
        representation_type="pdf",
        role="original",
        availability="available",
        path_ref=f"{name}.pdf",
        sha256="a" * 64,
    )
    session.add_all([record, representation])
    session.flush()
    chunk = DocumentChunk(
        asset_id=asset.id,
        representation_id=representation.id,
        chunk_index=0,
        chunk_kind="text",
        text_content=f"Body {name}",
        content_hash="b" * 64,
        parser_name="pdf",
        parser_version="1",
        anchor_kind="pdf_page",
        anchor={"page": 1},
    )
    session.add(chunk)
    session.add(Annotation(asset_id=asset.id, body_markdown=f"Note {name}"))
    session.commit()
    return asset, representation, chunk


def _state_snapshot(session: Session, ids: set[UUID]) -> list[tuple[Any, ...]]:
    return [
        (
            item.id,
            item.index_name,
            item.version,
            item.status,
            item.content_digest,
            item.updated_at,
            item.indexed_at,
            item.row_version,
        )
        for item in session.scalars(
            select(IndexState).where(IndexState.entity_id.in_(ids)).order_by(IndexState.id)
        ).all()
    ]


def test_refresh_removes_only_selected_asset_stale_chunks_and_preserves_other_states(
    session: Session,
) -> None:
    asset_a, rep_a, chunk_a = _seed(session, "a")
    asset_b, _, chunk_b = _seed(session, "b")
    suite = _ScopedSuite()
    builder = IndexBuilder(cast(RemoteIndexSuite, suite), write_batch_size=1)
    builder.refresh_keywords(session, asset_ids=(asset_a.id, asset_b.id))
    session.commit()
    b_ids = {
        asset_b.id,
        chunk_b.id,
        *session.scalars(select(Annotation.id).where(Annotation.asset_id == asset_b.id)).all(),
    }
    b_states = _state_snapshot(session, b_ids)
    b_documents = {
        name: {
            key: value
            for key, value in index.documents.items()
            if value.asset_id == str(asset_b.id)
        }
        for name, index in suite.keyword_indexes.items()
    }
    other_version = IndexState(
        index_name="documents",
        index_kind="keyword",
        entity_type="chunk",
        entity_id=chunk_a.id,
        content_digest="f" * 64,
        version="v2",
        status="ready",
    )
    session.add(other_version)
    session.execute(delete(DocumentChunk).where(DocumentChunk.id == chunk_a.id))
    replacement = DocumentChunk(
        asset_id=asset_a.id,
        representation_id=rep_a.id,
        chunk_index=0,
        chunk_kind="text",
        text_content="New body A",
        content_hash="c" * 64,
        parser_name="pdf",
        parser_version="1",
    )
    session.add(replacement)
    session.commit()
    suite.meili_transport.fetches.clear()

    report = builder.refresh_keywords(session, asset_ids=(asset_a.id,))
    session.commit()

    assert report.index_version == "v1"
    assert report.asset_ids == (str(asset_a.id),)
    assert report.stale_keyword_documents_removed["documents"] == 1
    assert report.scoped_keyword_counts_after == {"assets": 1, "documents": 1, "annotations": 1}
    assert all(asset_id == str(asset_a.id) for _, asset_id in suite.meili_transport.fetches)
    assert not any(name == "code" for name, _ in suite.meili_transport.fetches)
    assert f"chunk:{chunk_a.id}" not in suite.keyword_indexes["documents"].documents
    projected = suite.keyword_indexes["documents"].documents[f"chunk:{replacement.id}"]
    assert projected.kind == "document"
    assert projected.metadata["content_hash"] == "c" * 64
    assert projected.metadata["representation_digest"] == "a" * 64
    assert _state_snapshot(session, b_ids) == b_states
    preserved = session.get(IndexState, other_version.id)
    assert preserved is not None and preserved.status == "ready"
    for name, index in suite.keyword_indexes.items():
        assert {
            key: value
            for key, value in index.documents.items()
            if value.asset_id == str(asset_b.id)
        } == b_documents[name]
    old_state = session.scalar(
        select(IndexState).where(
            IndexState.entity_id == chunk_a.id,
            IndexState.version == "v1",
        )
    )
    assert old_state is not None and old_state.status == "stale"
    state_count = len(session.scalars(select(IndexState.id)).all())
    second = builder.refresh_keywords(session, asset_ids=(asset_a.id,))
    session.commit()
    assert second.stale_keyword_documents_removed == {"assets": 0, "documents": 0, "annotations": 0}
    assert second.scoped_keyword_counts_after == report.scoped_keyword_counts_after
    assert len(session.scalars(select(IndexState.id)).all()) == state_count


def test_refresh_uses_formal_projection_and_skips_code_by_default(session: Session) -> None:
    asset, representation, _ = _seed(session, "code")
    representation.representation_type = "repository"
    session.commit()
    suite = _ScopedSuite()
    formal = PostgresIndexCatalog().load(session, asset_ids=(asset.id,))
    report = IndexBuilder(cast(RemoteIndexSuite, suite)).refresh_keywords(
        session, asset_ids=(asset.id,)
    )
    assert report.skipped_indexes == ("code",)
    assert report.keyword_documents_upserted["documents"] == 0
    assert not suite.keyword_indexes["code"].documents
    for name in report.selected_indexes:
        assert (
            tuple(suite.keyword_indexes[name].documents.values()) == formal.keyword_documents[name]
        )
    assert all(state.version == "v1" for state in session.scalars(select(IndexState)).all())


def test_refresh_deleted_asset_cleans_remote_documents_without_local_chunks(
    session: Session,
) -> None:
    asset, _, chunk = _seed(session, "deleted")
    suite = _ScopedSuite()
    builder = IndexBuilder(cast(RemoteIndexSuite, suite))
    builder.refresh_keywords(session, asset_ids=(asset.id,))
    session.commit()
    asset.deleted_at = datetime.now(UTC)
    session.execute(delete(DocumentChunk).where(DocumentChunk.id == chunk.id))
    session.commit()
    report = builder.refresh_keywords(session, asset_ids=(asset.id,))
    assert report.keyword_documents_upserted == {"assets": 0, "documents": 0, "annotations": 0}
    assert report.stale_keyword_documents_removed == {"assets": 1, "documents": 1, "annotations": 1}
    assert all(not index.documents for index in suite.keyword_indexes.values())
    assert all(state.status == "stale" for state in session.scalars(select(IndexState)).all())


def test_refresh_retired_source_removes_representation_documents(session: Session) -> None:
    asset, representation, _ = _seed(session, "retired")
    session.add(
        Annotation(
            asset_id=asset.id, representation_id=representation.id, body_markdown="Anchored note"
        )
    )
    session.commit()
    suite = _ScopedSuite()
    builder = IndexBuilder(cast(RemoteIndexSuite, suite))
    builder.refresh_keywords(session, asset_ids=(asset.id,))
    session.commit()
    backup = BackupRun(destination_label="test")
    session.add(backup)
    session.flush()
    session.add(
        SourceRetirement(
            source_id=representation.source_id,
            backup_id=backup.id,
            payload={},
            payload_digest="a" * 64,
            frozen_manifest_sha256="a" * 64,
            frozen_source_config_sha256="a" * 64,
            record_set_digest="a" * 64,
            representation_set_digest="a" * 64,
            mount_set_digest="a" * 64,
            collection_digest="a" * 64,
            signature_algorithm="hmac-sha256",
            signer_key_id="test",
            signature="test",
            applied_by="pytest",
        )
    )
    session.commit()
    report = builder.refresh_keywords(session, asset_ids=(asset.id,))
    assert report.stale_keyword_documents_removed["documents"] == 1
    assert report.stale_keyword_documents_removed["annotations"] == 1
    assert len(suite.keyword_indexes["annotations"].documents) == 1
    assert (
        suite.keyword_indexes["assets"].documents[f"asset:{asset.id}"].metadata["has_pdf"] is False
    )


def test_remote_failure_rolls_back_ready_states_even_if_caller_commits(session: Session) -> None:
    asset, _, _ = _seed(session, "failed")
    suite = _ScopedSuite()
    suite.meili_transport.fail = True
    with pytest.raises(RuntimeError, match="remote task failed"):
        IndexBuilder(cast(RemoteIndexSuite, suite)).refresh_keywords(session, asset_ids=(asset.id,))
    session.commit()
    assert session.scalar(select(IndexState.id)) is None
    assert suite.meili_transport.wait_for_tasks is True


def test_source_scope_includes_materialized_repositories_and_intersects_explicit_ids(
    session: Session,
) -> None:
    asset, _, _ = _seed(session, "source")
    other, _, _ = _seed(session, "other")
    repository = Asset(asset_type="repository", title="Materialized repository")
    session.add(repository)
    session.flush()
    record = session.scalar(select(SourceRecord).where(SourceRecord.asset_id == asset.id))
    assert record is not None
    session.add(
        SourceRecordAsset(source_record_id=record.id, asset_id=repository.id, role="repository")
    )
    session.commit()
    source_scope = resolve_asset_scope(session, source_alias="source")
    assert source_scope is not None and set(source_scope) == {asset.id, repository.id}
    assert resolve_asset_scope(
        session, source_alias="source", asset_ids=(repository.id, other.id)
    ) == (repository.id,)
    assert resolve_asset_scope(session, asset_ids=()) == ()
    with pytest.raises(ValueError, match="Unknown source alias"):
        resolve_asset_scope(session, source_alias="unknown")


def test_refresh_spools_multiple_old_pages_before_deleting(session: Session) -> None:
    asset, representation, chunk = _seed(session, "pages")
    for index in range(1, 6):
        session.add(
            DocumentChunk(
                asset_id=asset.id,
                representation_id=representation.id,
                chunk_index=index,
                chunk_kind="text",
                text_content=f"Page {index}",
                content_hash=str(index) * 64,
                parser_name="pdf",
                parser_version="1",
            )
        )
    session.commit()
    suite = _ScopedSuite()
    builder = IndexBuilder(cast(RemoteIndexSuite, suite), write_batch_size=2)
    builder.refresh_keywords(session, asset_ids=(asset.id,))
    session.commit()
    session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.asset_id == asset.id,
            DocumentChunk.id != chunk.id,
        )
    )
    session.commit()
    report = builder.refresh_keywords(session, asset_ids=(asset.id,))
    assert report.scoped_keyword_counts_before is not None
    assert report.scoped_keyword_counts_before["documents"] == 6
    assert report.stale_keyword_documents_removed["documents"] == 5
    assert set(suite.keyword_indexes["documents"].documents) == {f"chunk:{chunk.id}"}


@pytest.mark.parametrize("missing_logical_id", [False, True])
def test_filtered_remote_response_cannot_expand_scope_or_hide_unaddressable_documents(
    session: Session,
    missing_logical_id: bool,
) -> None:
    asset, _, _ = _seed(session, "unsafe")
    suite = _ScopedSuite()

    class InvalidTransport(_ScopedTransport):
        def request(
            self, method: str, path: str, *, payload: Mapping[str, Any] | None = None
        ) -> Mapping[str, Any]:
            if path.endswith("/documents/fetch"):
                return {
                    "results": [
                        {
                            "asset_id": str(asset.id) if missing_logical_id else str(UUID(int=999)),
                            "logical_document_id": None
                            if missing_logical_id
                            else f"asset:{UUID(int=999)}",
                        }
                    ]
                }
            return super().request(method, path, payload=payload)

    suite.meili_transport = InvalidTransport(suite.keyword_indexes)
    suite.meili_admin = MeilisearchAdmin(cast(HttpxJSONTransport, suite.meili_transport))
    with pytest.raises(RuntimeError, match="outside the asset scope"):
        IndexBuilder(cast(RemoteIndexSuite, suite)).refresh_keywords(session, asset_ids=(asset.id,))
    session.commit()
    assert session.scalar(select(IndexState.id)) is None
    assert all(not index.documents for index in suite.keyword_indexes.values())


def test_optional_missing_code_index_reports_zero_without_provisioning() -> None:
    class MissingTransport:
        def request(self, _method: str, _path: str) -> Mapping[str, Any]:
            request = httpx.Request("GET", "http://meili.invalid/indexes/code/stats")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("index_not_found", request=request, response=response)

    assert (
        MeilisearchAdmin(cast(HttpxJSONTransport, MissingTransport())).document_count("code") == 0
    )
