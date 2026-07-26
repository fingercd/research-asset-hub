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
    EmbeddingStatus,
    HashingEmbedder,
    HybridSearchService,
    IndexBuilder,
    InMemoryKeywordIndex,
    InMemoryVectorIndex,
    PostgresIndexCatalog,
    RemoteIndexSuite,
    ResilientEmbedder,
    SearchDocument,
    SearchHit,
    SearchMode,
    VectorPoint,
    canonical_filters,
    meilisearch_filter,
    qdrant_filter,
)


class _UnavailableEmbedder:
    @property
    def status(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            provider="test",
            model="missing",
            dimension=32,
            available=False,
            detail="dependency missing",
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("dependency missing")


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


class _UnexpectedReranker:
    def rerank(self, *args: Any, **kwargs: Any) -> list[SearchHit]:
        raise AssertionError("semantic mode must preserve vector ordering")


class _RecordingEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    @property
    def status(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            provider="test",
            model="recording",
            dimension=2,
            available=True,
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


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
    asset_document = catalog.asset_documents[0]
    assert asset_document.asset_id == str(asset.id)
    assert "10.1000/world.model" in asset_document.exact_terms
    assert asset_document.metadata["domains"] == ["agent"]
    assert asset_document.metadata["reading_status"] == "reading"
    assert asset_document.metadata["has_code"] is True
    assert catalog.chunk_documents[0].exact_terms[-1] == "LatentPlanner"
    assert catalog.chunk_documents[0].metadata["chunk_id"]
    assert catalog.chunk_documents[0].metadata["chunk_hash"] == "a" * 64
    assert catalog.chunk_documents[0].metadata["representation_id"]
    assert catalog.chunk_documents[0].metadata["representation_digest"] is None
    assert catalog.chunk_documents[0].metadata["anchor"]["symbol"] == "LatentPlanner"


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
    qdrant = qdrant_filter(filters)
    assert len(qdrant["must"]) == 4


def test_embedding_fallback_is_explicitly_degraded() -> None:
    embedder = ResilientEmbedder(
        _UnavailableEmbedder(),
        HashingEmbedder(dimension=32),
    )
    assert len(embedder.encode(["世界模型"])[0]) == 32
    assert embedder.status.provider == "local-hashing"
    assert "degraded=true" in (embedder.status.detail or "")


def test_hybrid_search_protects_exact_identifier_and_filters() -> None:
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
    embedder = HashingEmbedder(dimension=32)
    vector = InMemoryVectorIndex()
    vector.upsert(
        [
            VectorPoint(
                point_id=exact.document_id,
                document=exact,
                vector=tuple(embedder.encode(["noise"])[0]),
            ),
            VectorPoint(
                point_id=semantic.document_id,
                document=semantic,
                vector=tuple(embedder.encode(["10.1000/exact"])[0]),
            ),
        ]
    )
    service = HybridSearchService(
        {"assets": keyword},
        asset_vectors=vector,
        embedder=embedder,
    )
    result = service.search("10.1000/exact", mode=SearchMode.HYBRID)
    assert result.hits[0].document.document_id == exact.document_id
    assert result.hits[0].exact_match is True
    filtered = service.search(
        "10.1000/exact",
        filters={"domain": "agent"},
    )
    assert {hit.document.asset_id for hit in filtered.hits} == {exact.asset_id}


def test_hybrid_search_can_expand_candidate_window_per_call() -> None:
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
    service = HybridSearchService({"assets": keyword}, candidate_limit=100)

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


class _MemoryMeiliAdmin:
    def __init__(self, indexes: dict[str, InMemoryKeywordIndex]) -> None:
        self.indexes = indexes

    def document_count(self, index_name: str) -> int:
        return len(self.indexes[index_name].documents)

    def document_ids(self, index_name: str) -> set[str]:
        return set(self.indexes[index_name].documents)


class _MemoryQdrantAdmin:
    def __init__(self, indexes: dict[str, InMemoryVectorIndex]) -> None:
        self.indexes = indexes

    def point_count(self, collection_name: str) -> int:
        return len(self.indexes[collection_name].points)

    def point_ids(self, collection_name: str) -> set[str]:
        return set(self.indexes[collection_name].points)


class _MemorySuite:
    def __init__(self) -> None:
        self.keyword_indexes = {
            name: InMemoryKeywordIndex() for name in ("assets", "documents", "code", "annotations")
        }
        self.asset_collection = "rah_assets_v1"
        self.chunk_collection = "rah_chunks_v1"
        self.asset_vectors = InMemoryVectorIndex()
        self.chunk_vectors = InMemoryVectorIndex()
        self.meili_admin = _MemoryMeiliAdmin(self.keyword_indexes)
        self.qdrant_admin = _MemoryQdrantAdmin(
            {
                self.asset_collection: self.asset_vectors,
                self.chunk_collection: self.chunk_vectors,
            }
        )


def test_builder_bounds_text_before_embedding_tokenization() -> None:
    embedder = _RecordingEmbedder()
    builder = IndexBuilder(
        cast(RemoteIndexSuite, _MemorySuite()),
        embedder=embedder,
    )
    document = SearchDocument(
        document_id="large",
        asset_id="asset",
        kind="code",
        title="Title",
        text="x" * 100_000,
    )

    builder._embed((document,))

    assert len(embedder.texts[0]) == len("Title\n") + 32_768


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
    service = HybridSearchService({"assets": assets, "code": code})

    result = service.search(
        "10.1000/exact",
        mode=SearchMode.KEYWORD,
        rerank=False,
    )

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
    service = HybridSearchService({"documents": documents})

    result = service.search(
        "shared query",
        mode=SearchMode.KEYWORD,
        rerank=False,
    )

    assert [hit.document.asset_id for hit in result.hits] == ["same-asset"]


def test_semantic_mode_does_not_apply_hybrid_reranker() -> None:
    assets = InMemoryKeywordIndex()
    assets.upsert(
        [
            SearchDocument(
                document_id="asset:one",
                asset_id="one",
                kind="asset",
                title="10.1000/semantic",
                text="summary",
                exact_terms=("10.1000/semantic",),
            )
        ]
    )
    service = HybridSearchService(
        {"assets": assets},
        reranker=cast(Any, _UnexpectedReranker()),
    )

    result = service.search(
        "10.1000/semantic",
        mode=SearchMode.SEMANTIC,
    )

    assert result.hits[0].document.asset_id == "one"
    assert result.reranker_status is None


def test_keyword_only_rebuild_does_not_write_vectors() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    suite = cast(RemoteIndexSuite, _MemorySuite())
    with Session(engine) as session:
        _seed(session)
        report = IndexBuilder(
            suite,
            embedder=HashingEmbedder(dimension=32),
        ).rebuild_keywords(session)

    assert report.keyword_counts_after == {
        "assets": 1,
        "documents": 0,
        "code": 1,
        "annotations": 1,
    }
    assert suite.asset_vectors.points == {}
    assert suite.chunk_vectors.points == {}


def test_builder_is_idempotent_and_index_state_does_not_duplicate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    suite = cast(RemoteIndexSuite, _MemorySuite())
    with Session(engine) as session:
        _seed(session)
        builder = IndexBuilder(
            suite,
            embedder=HashingEmbedder(dimension=32),
            version="v1",
            batch_size=2,
        )
        first = builder.build(session)
        session.commit()
        states_after_first = session.scalar(select(func.count()).select_from(IndexState))
        second = builder.build(session)
        session.commit()
        states_after_second = session.scalar(select(func.count()).select_from(IndexState))
        resumed = builder.build(session, resume_vectors=True)
        session.commit()
        states_after_resume = session.scalar(select(func.count()).select_from(IndexState))

    assert first.keyword_counts_after == second.keyword_counts_after
    assert first.vector_counts_after == second.vector_counts_after
    assert states_after_first == states_after_second == states_after_resume
    assert resumed.keyword_documents_upserted == {
        "assets": 0,
        "documents": 0,
        "code": 0,
        "annotations": 0,
    }
    assert resumed.vector_points_upserted["rah_assets_v1"] == 0
    assert resumed.vector_points_upserted["rah_chunks_v1"] == 0
    assert second.stale_keyword_documents_removed == {
        "assets": 0,
        "documents": 0,
        "code": 0,
        "annotations": 0,
    }
