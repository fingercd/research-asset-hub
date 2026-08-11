from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

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
        }
    )
    assert filters["domains"] == ["agent", "infra"]
    assert filters["asset_type"] == "paper"
    assert filters["has_pdf"] is True
    meili = meilisearch_filter(filters)
    assert 'metadata.domains IN ["agent", "infra"]' in meili
    assert "metadata.year >= 2020" in meili


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
