from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from packages.domain.models import (
    Annotation,
    Asset,
    Base,
    DocumentChunk,
    ExternalIdentifierModel,
    IndexState,
    ReadingState,
    Representation,
)
from packages.indexing import (
    IndexBuilder,
    InMemoryKeywordIndex,
    KeywordSearchService,
    PostgresIndexCatalog,
    RemoteIndexSuite,
    SearchDocument,
    SearchHit,
    SearchMode,
    canonical_filters,
    meilisearch_filter,
)


class _CountingKeywordIndex(InMemoryKeywordIndex):
    def __init__(self) -> None:
        super().__init__()
        self.search_calls = 0
        self.last_limit: int | None = None

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        self.search_calls += 1
        self.last_limit = limit
        return super().search(
            query,
            limit=limit,
            filters=filters,
        )


def _seed(session: Session) -> Asset:
    asset = Asset(
        asset_type="paper",
        title="Latent World Models",
        title_zh="潜在世界模型",
        authors=["Ada Lovelace"],
        year=2025,
        abstract="Latent dynamics for agents.",
        domain_slugs=["agent"],
        tags=["world-model"],
    )
    session.add(asset)
    session.flush()
    session.add_all(
        [
            ExternalIdentifierModel(
                asset_id=asset.id,
                scheme="doi",
                value="10.1000/World.Model",
                canonical="10.1000/world.model",
            ),
            ReadingState(asset_id=asset.id, user_id="local", status="reading"),
        ]
    )
    representation = Representation(
        asset_id=asset.id,
        representation_type="source_code",
        role="original",
        availability="available",
        root_key="source",
        path_ref="code/model.py",
    )
    session.add(representation)
    session.flush()
    session.add(
        DocumentChunk(
            asset_id=asset.id,
            representation_id=representation.id,
            chunk_index=0,
            chunk_kind="code",
            text_content="class LatentPlanner: pass",
            content_hash="a" * 64,
            parser_name="tree-sitter",
            parser_version="1",
            anchor_kind="code_lines",
            anchor={"symbol": "LatentPlanner", "start_line": 1, "end_line": 1},
        )
    )
    session.add(
        Annotation(
            asset_id=asset.id,
            kind="comment",
            summary="值得精读",
            body_markdown="核心机制值得精读。",
        )
    )
    session.flush()
    return asset


def test_catalog_routes_assets_code_and_annotations() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        asset = _seed(session)
        catalog = PostgresIndexCatalog().load(session)

    assert len(catalog.keyword_documents["assets"]) == 1
    assert len(catalog.keyword_documents["documents"]) == 0
    assert len(catalog.keyword_documents["code"]) == 1
    assert len(catalog.keyword_documents["annotations"]) == 1
    asset_document = catalog.keyword_documents["assets"][0]
    assert asset_document.asset_id == str(asset.id)
    assert "10.1000/world.model" in asset_document.exact_terms
    assert asset_document.metadata["domains"] == ["agent"]
    assert asset_document.metadata["reading_status"] == "reading"
    assert asset_document.metadata["has_code"] is True
    code_document = catalog.keyword_documents["code"][0]
    assert code_document.exact_terms[-1] == "LatentPlanner"
    assert code_document.metadata["chunk_id"]
    assert code_document.metadata["chunk_hash"] == "a" * 64
    assert code_document.metadata["representation_id"]
    assert code_document.metadata["anchor"]["symbol"] == "LatentPlanner"


def test_filters_translate_consistently() -> None:
    filters = canonical_filters(
        {
            "domain": ["agent", "infra"],
            "type": "paper",
            "year_from": 2020,
            "year_to": 2025,
            "pdf": "true",
            "asset_id": ["paper-1", "repository-1"],
        }
    )
    assert filters["domains"] == ["agent", "infra"]
    assert filters["asset_type"] == "paper"
    assert filters["has_pdf"] is True
    assert filters["asset_id"] == ["paper-1", "repository-1"]
    meili = meilisearch_filter(filters)
    assert 'metadata.domains IN ["agent", "infra"]' in meili
    assert "metadata.year >= 2020" in meili
    assert 'asset_id IN ["paper-1", "repository-1"]' in meili
    assert not any("metadata.asset_id" in clause for clause in meili)
    assert meilisearch_filter({"asset_id": []}) == ["asset_id IN []"]


def test_keyword_search_protects_exact_identifier_and_filters() -> None:
    exact = SearchDocument(
        document_id="asset:00000000-0000-0000-0000-000000000001",
        asset_id="00000000-0000-0000-0000-000000000001",
        kind="asset",
        title="An unrelated title",
        text="minimal",
        metadata={"domains": ["agent"], "asset_type": "paper", "has_pdf": True},
        exact_terms=("10.1000/exact",),
    )
    semantic = SearchDocument(
        document_id="asset:00000000-0000-0000-0000-000000000002",
        asset_id="00000000-0000-0000-0000-000000000002",
        kind="asset",
        title="10.1000 exact discussion",
        text="10.1000/exact",
        metadata={"domains": ["infra"], "asset_type": "paper", "has_pdf": True},
    )
    keyword = InMemoryKeywordIndex()
    keyword.upsert([exact, semantic])
    service = KeywordSearchService({"assets": keyword})
    result = service.search("10.1000/exact", mode=SearchMode.KEYWORD)
    assert result.hits[0].document.document_id == exact.document_id
    assert result.hits[0].exact_match is True
    filtered = service.search(
        "10.1000/exact",
        filters={"domain": "agent"},
    )
    assert {hit.document.asset_id for hit in filtered.hits} == {exact.asset_id}

    by_asset_id = service.search(
        "10.1000/exact",
        filters={"asset_id": semantic.asset_id},
    )
    assert {hit.document.asset_id for hit in by_asset_id.hits} == {semantic.asset_id}


def test_keyword_search_can_expand_candidate_window_per_call() -> None:
    keyword = _CountingKeywordIndex()
    keyword.upsert(
        [
            SearchDocument(
                document_id=f"asset:{index:03d}",
                asset_id=f"{index:032d}",
                kind="asset",
                title=f"Wide candidate {index:03d}",
                text="wide candidate",
            )
            for index in range(121)
        ]
    )
    service = KeywordSearchService({"assets": keyword}, candidate_limit=100)

    default = service.search("wide candidate", mode=SearchMode.KEYWORD, limit=121)
    assert len(default.hits) == 100
    assert keyword.last_limit == 100

    expanded = service.search(
        "wide candidate",
        mode=SearchMode.KEYWORD,
        limit=121,
        candidate_limit=121,
    )
    assert len(expanded.hits) == 121
    assert keyword.last_limit == 121


def test_keyword_search_short_circuits_after_exact_asset_match() -> None:
    assets = _CountingKeywordIndex()
    code = _CountingKeywordIndex()
    assets.upsert(
        [
            SearchDocument(
                document_id="asset:one",
                asset_id="one",
                kind="asset",
                title="Exact Paper Title",
                text="summary",
                exact_terms=("10.1000/exact",),
            )
        ]
    )
    code.upsert(
        [
            SearchDocument(
                document_id="chunk:one",
                asset_id="one",
                kind="code",
                title="Code",
                text="10.1000/exact",
            )
        ]
    )
    service = KeywordSearchService({"assets": assets, "code": code})

    result = service.search("10.1000/exact", mode=SearchMode.KEYWORD)

    assert result.hits[0].document.asset_id == "one"
    assert assets.search_calls == 1
    assert code.search_calls == 0


def test_search_deduplicates_chunks_by_asset_before_returning() -> None:
    documents = InMemoryKeywordIndex()
    documents.upsert(
        [
            SearchDocument(
                document_id=f"chunk:{index}",
                asset_id="same-asset",
                kind="document",
                title="Repeated",
                text="shared query",
            )
            for index in range(3)
        ]
    )
    service = KeywordSearchService({"documents": documents})

    result = service.search("shared query", mode=SearchMode.KEYWORD)

    assert [hit.document.asset_id for hit in result.hits] == ["same-asset"]


class _MemoryMeiliAdmin:
    def __init__(self, indexes: dict[str, InMemoryKeywordIndex]) -> None:
        self.indexes = indexes

    def document_count(self, index_name: str) -> int:
        return len(self.indexes[index_name].documents)

    def document_ids(self, index_name: str) -> set[str]:
        return set(self.indexes[index_name].documents)


class _MemoryTransport:
    wait_for_tasks = False

    def flush_pending_tasks(self) -> None:
        return None


class _MemorySuite:
    def __init__(self) -> None:
        self.keyword_indexes = {
            name: InMemoryKeywordIndex() for name in ("assets", "documents", "code", "annotations")
        }
        self.meili_admin = _MemoryMeiliAdmin(self.keyword_indexes)
        self.meili_transport = _MemoryTransport()


class _GeneratedCatalog:
    def __init__(self, total: int) -> None:
        self.total = total
        self.scopes: list[str] = []
        self.max_generated_batch = 0

    def iter_index_batches(
        self,
        _session: Session,
        *,
        index_name: str,
        user_id: str,
        scope: str,
        batch_size: int,
    ) -> Iterable[list[SearchDocument]]:
        assert user_id == "local"
        self.scopes.append(scope)
        entity_type = {
            "assets": "asset",
            "documents": "chunk",
            "code": "chunk",
            "annotations": "annotation",
        }[index_name]
        identifier_offset = {
            "assets": 0,
            "documents": 10_000,
            "code": 20_000,
            "annotations": 30_000,
        }[index_name]
        for start in range(0, self.total, batch_size):
            batch = [
                SearchDocument(
                    document_id=(
                        f"{entity_type}:{UUID(int=identifier_offset + index + 1)}"
                    ),
                    asset_id=str(UUID(int=identifier_offset + index + 1)),
                    kind=index_name.removesuffix("s"),
                    title=f"Generated {index}",
                    text="x" * 4096,
                )
                for index in range(start, min(start + batch_size, self.total))
            ]
            self.max_generated_batch = max(self.max_generated_batch, len(batch))
            yield batch


class _PagedRemoteTransport:
    def __init__(self) -> None:
        self.wait_for_tasks = False
        self.documents: dict[str, set[str]] = {
            name: set() for name in ("assets", "documents", "code", "annotations")
        }
        self.flush_calls = 0
        self.fail_flush = False
        self.max_remote_page = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        del payload
        assert method == "GET"
        index_name = path.split("/", 3)[2]
        query = path.partition("?")[2]
        parameters = dict(part.split("=", 1) for part in query.split("&"))
        limit = int(parameters["limit"])
        offset = int(parameters["offset"])
        values = sorted(self.documents[index_name])[offset : offset + limit]
        self.max_remote_page = max(self.max_remote_page, len(values))
        return {"results": [{"logical_document_id": value} for value in values]}

    def flush_pending_tasks(self) -> None:
        self.flush_calls += 1
        if self.fail_flush:
            raise RuntimeError("synthetic Meilisearch task failure")


class _PagedRemoteIndex:
    def __init__(self, transport: _PagedRemoteTransport, index_name: str) -> None:
        self.transport = transport
        self.index_name = index_name
        self.max_upsert_batch = 0
        self.max_delete_batch = 0

    def upsert(self, documents: Sequence[SearchDocument]) -> None:
        self.max_upsert_batch = max(self.max_upsert_batch, len(documents))
        self.transport.documents[self.index_name].update(
            document.document_id for document in documents
        )

    def delete(self, document_ids: Sequence[str]) -> None:
        self.max_delete_batch = max(self.max_delete_batch, len(document_ids))
        self.transport.documents[self.index_name].difference_update(document_ids)


class _PagedRemoteAdmin:
    def __init__(self, transport: _PagedRemoteTransport) -> None:
        self.transport = transport

    def document_count(self, index_name: str) -> int:
        return len(self.transport.documents[index_name])


class _PagedRemoteSuite:
    def __init__(self) -> None:
        self.meili_transport = _PagedRemoteTransport()
        self.meili_admin = _PagedRemoteAdmin(self.meili_transport)
        self.keyword_indexes = {
            name: _PagedRemoteIndex(self.meili_transport, name)
            for name in ("assets", "documents", "code", "annotations")
        }


def test_keyword_rebuild_writes_only_keyword_indexes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    suite = cast(RemoteIndexSuite, _MemorySuite())
    with Session(engine) as session:
        _seed(session)
        report = IndexBuilder(suite).rebuild_keywords(session)

    assert report.keyword_counts_after == {
        "assets": 1,
        "documents": 0,
        "code": 1,
        "annotations": 1,
    }


def test_keyword_rebuild_can_leave_code_index_untouched() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    memory_suite = _MemorySuite()
    memory_suite.keyword_indexes["code"].upsert(
        [
            SearchDocument(
                document_id="code:preserved",
                asset_id="preserved",
                kind="code",
                title="Preserved code index",
                text="not rebuilt",
            )
        ]
    )
    suite = cast(RemoteIndexSuite, memory_suite)
    with Session(engine) as session:
        _seed(session)
        report = IndexBuilder(suite).rebuild_keywords(
            session,
            index_names=("assets", "documents", "annotations"),
        )

    assert report.selected_indexes == ("assets", "documents", "annotations")
    assert report.skipped_indexes == ("code",)
    assert report.keyword_counts_before["code"] == 1
    assert report.keyword_counts_after["code"] == 1
    assert report.keyword_documents_upserted["code"] == 0
    assert report.stale_keyword_documents_removed["code"] == 0
    assert set(memory_suite.keyword_indexes["code"].documents) == {"code:preserved"}


def test_builder_is_idempotent_and_index_state_does_not_duplicate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    suite = cast(RemoteIndexSuite, _MemorySuite())
    with Session(engine) as session:
        _seed(session)
        builder = IndexBuilder(
            suite,
            version="v1",
        )
        first = builder.rebuild_keywords(session)
        session.commit()
        states_after_first = session.scalar(select(func.count()).select_from(IndexState))
        second = builder.rebuild_keywords(session)
        session.commit()
        states_after_second = session.scalar(select(func.count()).select_from(IndexState))

    assert first.keyword_counts_after == second.keyword_counts_after
    assert states_after_first == states_after_second
    assert second.keyword_documents_upserted == {
        "assets": 1,
        "documents": 0,
        "code": 1,
        "annotations": 1,
    }
    assert second.stale_keyword_documents_removed == {
        "assets": 0,
        "documents": 0,
        "code": 0,
        "annotations": 0,
    }


def test_streaming_rebuild_bounds_batches_and_spools_stale_ids() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    suite = _PagedRemoteSuite()
    catalog = _GeneratedCatalog(total=503)
    stale_ids = {f"asset:{UUID(int=100_000 + index)}" for index in range(311)}
    suite.meili_transport.documents["assets"].update(stale_ids)

    with Session(engine) as session:
        report = IndexBuilder(
            cast(RemoteIndexSuite, suite),
            write_batch_size=37,
            task_flush_batch_count=3,
            catalog=cast(PostgresIndexCatalog, catalog),
        ).rebuild_keywords(session)
        session.commit()
        ready_states = session.scalar(
            select(func.count())
            .select_from(IndexState)
            .where(IndexState.status == "ready")
        )

    assert report.keyword_documents_upserted == {
        "assets": 503,
        "documents": 503,
        "code": 503,
        "annotations": 503,
    }
    assert report.stale_keyword_documents_removed["assets"] == len(stale_ids)
    assert report.keyword_counts_after == {
        "assets": 503,
        "documents": 503,
        "code": 503,
        "annotations": 503,
    }
    assert report.batch_size == 37
    assert report.peak_buffered_documents == 37
    assert catalog.max_generated_batch == 37
    assert suite.keyword_indexes["assets"].max_upsert_batch == 37
    assert suite.keyword_indexes["assets"].max_delete_batch <= 37
    assert suite.meili_transport.max_remote_page <= 37
    assert set(catalog.scopes) == {"serving_active"}
    assert ready_states == 2_012
    engine.dispose()


def test_failed_remote_task_rolls_back_index_state_and_is_rerunnable() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    suite = _PagedRemoteSuite()
    catalog = _GeneratedCatalog(total=5)
    existing_id = UUID(int=900_000)
    with Session(engine) as session:
        session.add(
            IndexState(
                index_name="assets",
                index_kind="keyword",
                entity_type="asset",
                entity_id=existing_id,
                content_digest="a" * 64,
                status="ready",
                version="v1",
            )
        )
        session.commit()
        suite.meili_transport.fail_flush = True
        builder = IndexBuilder(
            cast(RemoteIndexSuite, suite),
            write_batch_size=2,
            task_flush_batch_count=1,
            catalog=cast(PostgresIndexCatalog, catalog),
        )
        with pytest.raises(RuntimeError, match="synthetic Meilisearch task failure"):
            builder.rebuild_keywords(session)
        session.commit()
        persisted = session.scalar(
            select(IndexState).where(IndexState.entity_id == existing_id)
        )
        assert persisted is not None
        assert persisted.status == "ready"
        assert session.scalar(select(func.count()).select_from(IndexState)) == 1

        suite.meili_transport.fail_flush = False
        report = builder.rebuild_keywords(session)
        session.commit()

    assert report.keyword_counts_after == {
        "assets": 5,
        "documents": 5,
        "code": 5,
        "annotations": 5,
    }
    assert report.peak_buffered_documents == 2
    engine.dispose()
