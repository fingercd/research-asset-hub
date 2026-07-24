"""Build the four keyword indexes and two versioned vector collections."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.enums import IndexKind, IndexStatus, ReadingStatus
from packages.domain.models import (
    Annotation,
    Asset,
    DocumentChunk,
    ExternalIdentifierModel,
    IndexState,
    ReadingState,
    Representation,
)

from .embeddings import Embedder, HashingEmbedder, ResilientEmbedder
from .hybrid import HybridSearchService
from .meili import (
    HttpxJSONTransport,
    KeywordIndex,
    MeilisearchAdmin,
    MeilisearchIndex,
)
from .models import SearchDocument, VectorPoint
from .qdrant import QdrantAdmin, QdrantIndex, VectorIndex
from .rerank import Reranker

KEYWORD_INDEX_NAMES = ("assets", "documents", "code", "annotations")
DEFAULT_VECTOR_VERSION = "v1"
MAX_VECTOR_CODE_CHUNKS_PER_ASSET = 200
MAX_EMBEDDING_TEXT_CHARS = 32_768
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class IndexCatalog:
    keyword_documents: Mapping[str, tuple[SearchDocument, ...]]
    asset_documents: tuple[SearchDocument, ...]
    chunk_documents: tuple[SearchDocument, ...]


@dataclass(frozen=True, slots=True)
class IndexBuildReport:
    index_version: str
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    degraded: bool
    keyword_counts_before: Mapping[str, int]
    keyword_counts_after: Mapping[str, int]
    vector_counts_before: Mapping[str, int]
    vector_counts_after: Mapping[str, int]
    keyword_documents_upserted: Mapping[str, int]
    vector_points_upserted: Mapping[str, int]
    stale_keyword_documents_removed: Mapping[str, int]
    stale_vector_points_removed: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class KeywordBuildReport:
    keyword_counts_before: Mapping[str, int]
    keyword_counts_after: Mapping[str, int]
    keyword_documents_upserted: Mapping[str, int]
    stale_keyword_documents_removed: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RemoteIndexSuite:
    keyword_indexes: dict[str, KeywordIndex]
    asset_vectors: VectorIndex
    chunk_vectors: VectorIndex
    meili_admin: MeilisearchAdmin
    qdrant_admin: QdrantAdmin
    meili_transport: HttpxJSONTransport
    qdrant_transport: HttpxJSONTransport
    asset_collection: str
    chunk_collection: str

    @classmethod
    def connect(
        cls,
        *,
        meilisearch_url: str,
        meilisearch_api_key: str | None,
        qdrant_url: str,
        dimension: int = 1024,
        version: str = DEFAULT_VECTOR_VERSION,
    ) -> RemoteIndexSuite:
        safe_version = _safe_version(version)
        asset_collection = f"rah_assets_{safe_version}"
        chunk_collection = f"rah_chunks_{safe_version}"
        meili_transport = HttpxJSONTransport(
            meilisearch_url,
            api_key=meilisearch_api_key,
            timeout=30,
        )
        qdrant_transport = HttpxJSONTransport(qdrant_url, timeout=30)
        meili_admin = MeilisearchAdmin(meili_transport)
        qdrant_admin = QdrantAdmin(qdrant_transport)
        for index_name in KEYWORD_INDEX_NAMES:
            meili_admin.ensure_index(index_name)
        qdrant_admin.ensure_collection(asset_collection, dimension=dimension)
        qdrant_admin.ensure_collection(chunk_collection, dimension=dimension)
        return cls(
            keyword_indexes={
                index_name: MeilisearchIndex(
                    meili_transport,
                    index_name=index_name,
                )
                for index_name in KEYWORD_INDEX_NAMES
            },
            asset_vectors=QdrantIndex(
                qdrant_transport,
                collection_name=asset_collection,
                dimension=dimension,
            ),
            chunk_vectors=QdrantIndex(
                qdrant_transport,
                collection_name=chunk_collection,
                dimension=dimension,
            ),
            meili_admin=meili_admin,
            qdrant_admin=qdrant_admin,
            meili_transport=meili_transport,
            qdrant_transport=qdrant_transport,
            asset_collection=asset_collection,
            chunk_collection=chunk_collection,
        )

    def close(self) -> None:
        self.meili_transport.close()
        self.qdrant_transport.close()

    def search_service(
        self,
        *,
        embedder: Embedder,
        reranker: Reranker | None = None,
    ) -> HybridSearchService:
        return HybridSearchService(
            self.keyword_indexes,
            asset_vectors=self.asset_vectors,
            chunk_vectors=self.chunk_vectors,
            embedder=embedder,
            reranker=reranker,
        )


class PostgresIndexCatalog:
    """Materialize backend-neutral documents from the operational database."""

    def load(self, session: Session, *, user_id: str = "local") -> IndexCatalog:
        assets = list(
            session.scalars(
                select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.id)
            ).all()
        )
        asset_by_id = {asset.id: asset for asset in assets}
        identifiers: defaultdict[UUID, list[str]] = defaultdict(list)
        for asset_id, value, canonical in session.execute(
            select(
                ExternalIdentifierModel.asset_id,
                ExternalIdentifierModel.value,
                ExternalIdentifierModel.canonical,
            ).where(ExternalIdentifierModel.asset_id.in_(asset_by_id))
        ):
            identifiers[asset_id].extend((str(canonical), str(value)))

        reading_status = {
            asset_id: status
            for asset_id, status in session.execute(
                select(ReadingState.asset_id, ReadingState.status).where(
                    ReadingState.user_id == user_id,
                    ReadingState.asset_id.in_(asset_by_id),
                )
            )
        }
        representation_types: defaultdict[UUID, set[str]] = defaultdict(set)
        available_representation_types: defaultdict[UUID, set[str]] = defaultdict(set)
        representation_availability: defaultdict[UUID, set[str]] = defaultdict(set)
        representation_type_by_id: dict[UUID, str] = {}
        representation_digest_by_id: dict[UUID, str | None] = {}
        for (
            representation_id,
            asset_id,
            rep_type,
            availability,
            sha256,
            tree_digest,
            revision,
        ) in session.execute(
            select(
                Representation.id,
                Representation.asset_id,
                Representation.representation_type,
                Representation.availability,
                Representation.sha256,
                Representation.tree_digest,
                Representation.revision,
            ).where(
                Representation.deleted_at.is_(None),
                Representation.asset_id.in_(asset_by_id),
            )
        ):
            representation_types[asset_id].add(str(rep_type))
            representation_availability[asset_id].add(str(availability))
            if str(availability) == "available":
                available_representation_types[asset_id].add(str(rep_type))
            representation_type_by_id[representation_id] = str(rep_type)
            representation_digest_by_id[representation_id] = (
                str(sha256 or tree_digest or revision)
                if sha256 or tree_digest or revision
                else None
            )

        asset_documents: list[SearchDocument] = []
        metadata_by_asset: dict[UUID, dict[str, Any]] = {}
        exact_by_asset: dict[UUID, tuple[str, ...]] = {}
        for asset in assets:
            rep_types = sorted(representation_types[asset.id])
            metadata = _asset_metadata(
                asset,
                reading_status=str(reading_status.get(asset.id, ReadingStatus.UNREAD.value)),
                representation_types=rep_types,
                available_representation_types=sorted(available_representation_types[asset.id]),
                availability=sorted(representation_availability[asset.id]),
            )
            exact_terms = _unique_terms(
                asset.title,
                asset.title_zh,
                *list(asset.authors or []),
                *list(asset.source_urls or []),
                *identifiers[asset.id],
            )
            metadata_by_asset[asset.id] = metadata
            exact_by_asset[asset.id] = exact_terms
            asset_documents.append(
                SearchDocument(
                    document_id=f"asset:{asset.id}",
                    asset_id=str(asset.id),
                    kind="asset",
                    title=asset.title,
                    text="\n".join(
                        str(value)
                        for value in (
                            asset.title_zh,
                            asset.subtitle,
                            asset.venue,
                            asset.abstract,
                            asset.summary,
                            asset.content,
                            " ".join(asset.authors or []),
                            " ".join(asset.tags or []),
                        )
                        if value
                    ),
                    metadata=metadata,
                    exact_terms=exact_terms,
                )
            )

        document_chunks: list[SearchDocument] = []
        code_chunks: list[SearchDocument] = []
        for chunk in session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.asset_id.in_(asset_by_id))
            .order_by(DocumentChunk.id)
        ):
            asset = asset_by_id[chunk.asset_id]
            rep_type = representation_type_by_id.get(chunk.representation_id, "")
            is_code = _is_code_chunk(chunk.chunk_kind, chunk.anchor_kind, rep_type)
            metadata = {
                **metadata_by_asset[chunk.asset_id],
                "representation_id": str(chunk.representation_id),
                "representation_digest": representation_digest_by_id.get(chunk.representation_id),
                "chunk_id": str(chunk.id),
                "chunk_index": chunk.chunk_index,
                "chunk_kind": chunk.chunk_kind,
                "anchor_kind": chunk.anchor_kind,
                "anchor": dict(chunk.anchor or {}),
                "chunk_hash": chunk.content_hash,
                "content_hash": chunk.content_hash,
                "parser_name": chunk.parser_name,
                "parser_version": chunk.parser_version,
            }
            exact_terms = _anchor_exact_terms(chunk.anchor)
            document = SearchDocument(
                document_id=f"chunk:{chunk.id}",
                asset_id=str(chunk.asset_id),
                kind="code" if is_code else "document",
                title=asset.title,
                text=chunk.text_content,
                metadata=metadata,
                exact_terms=exact_terms,
            )
            (code_chunks if is_code else document_chunks).append(document)

        annotation_documents: list[SearchDocument] = []
        for annotation in session.scalars(
            select(Annotation)
            .where(
                Annotation.deleted_at.is_(None),
                Annotation.asset_id.in_(asset_by_id),
            )
            .order_by(Annotation.id)
        ):
            asset = asset_by_id[annotation.asset_id]
            annotation_documents.append(
                SearchDocument(
                    document_id=f"annotation:{annotation.id}",
                    asset_id=str(annotation.asset_id),
                    kind="annotation",
                    title=annotation.title or asset.title,
                    text="\n".join(
                        value for value in (annotation.summary, annotation.body_markdown) if value
                    ),
                    metadata={
                        **metadata_by_asset[annotation.asset_id],
                        "annotation_kind": annotation.kind,
                        "pinned": annotation.pinned,
                        "representation_id": (
                            str(annotation.representation_id)
                            if annotation.representation_id
                            else None
                        ),
                        "representation_digest": (
                            representation_digest_by_id.get(annotation.representation_id)
                            if annotation.representation_id
                            else None
                        ),
                    },
                    exact_terms=_unique_terms(annotation.title),
                )
            )
        semantic_code_chunks = _semantic_code_documents(code_chunks)
        all_chunks = [*document_chunks, *semantic_code_chunks]
        return IndexCatalog(
            keyword_documents={
                "assets": tuple(asset_documents),
                "documents": tuple(document_chunks),
                "code": tuple(code_chunks),
                "annotations": tuple(annotation_documents),
            },
            asset_documents=tuple(asset_documents),
            chunk_documents=tuple(all_chunks),
        )


class IndexBuilder:
    def __init__(
        self,
        suite: RemoteIndexSuite,
        *,
        embedder: Embedder | None = None,
        version: str = DEFAULT_VECTOR_VERSION,
        batch_size: int = 32,
        write_batch_size: int = 256,
        vector_write_batch_size: int = 128,
    ) -> None:
        self.suite = suite
        self.embedder = embedder or ResilientEmbedder()
        self.version = version
        self.batch_size = max(1, batch_size)
        self.write_batch_size = max(1, write_batch_size)
        self.vector_write_batch_size = max(1, vector_write_batch_size)

    def build(
        self,
        session: Session,
        *,
        user_id: str = "local",
        resume_vectors: bool = False,
    ) -> IndexBuildReport:
        catalog = PostgresIndexCatalog().load(session, user_id=user_id)
        keyword_before = {
            name: self.suite.meili_admin.document_count(name) for name in KEYWORD_INDEX_NAMES
        }
        vector_before = {
            self.suite.asset_collection: self.suite.qdrant_admin.point_count(
                self.suite.asset_collection
            ),
            self.suite.chunk_collection: self.suite.qdrant_admin.point_count(
                self.suite.chunk_collection
            ),
        }
        expected_keyword_ids = {
            name: {document.document_id for document in documents}
            for name, documents in catalog.keyword_documents.items()
        }
        expected_asset_points = {
            _point_id(document.document_id) for document in catalog.asset_documents
        }
        expected_chunk_points = {
            _point_id(document.document_id) for document in catalog.chunk_documents
        }

        removed_keyword: dict[str, int] = {}
        keyword_upserts: dict[str, int] = {}
        if resume_vectors:
            mismatched_indexes = [
                name
                for name, expected_ids in expected_keyword_ids.items()
                if self.suite.meili_admin.document_ids(name) != expected_ids
            ]
            if mismatched_indexes:
                raise RuntimeError(
                    "Cannot resume vector build because keyword indexes differ: "
                    + ", ".join(sorted(mismatched_indexes))
                )
            for name in catalog.keyword_documents:
                removed_keyword[name] = 0
                keyword_upserts[name] = 0
            actual_asset_points = self.suite.qdrant_admin.point_ids(self.suite.asset_collection)
            if actual_asset_points != expected_asset_points:
                raise RuntimeError(
                    "Cannot resume vector build because asset vectors are incomplete."
                )
            stale_asset_points: set[str] = set()
        else:
            removed_keyword, keyword_upserts = self._rebuild_keyword_documents(
                catalog,
                expected_keyword_ids,
            )
            stale_asset_points = (
                self.suite.qdrant_admin.point_ids(self.suite.asset_collection)
                - expected_asset_points
            )

        actual_chunk_points = self.suite.qdrant_admin.point_ids(
            self.suite.chunk_collection
        )
        stale_chunk_points = actual_chunk_points - expected_chunk_points
        chunk_documents_to_upsert = (
            tuple(
                document
                for document in catalog.chunk_documents
                if _point_id(document.document_id) not in actual_chunk_points
            )
            if resume_vectors
            else catalog.chunk_documents
        )
        if stale_asset_points:
            self.suite.asset_vectors.delete(sorted(stale_asset_points))
        if stale_chunk_points:
            self.suite.chunk_vectors.delete(sorted(stale_chunk_points))
        if not resume_vectors:
            self._embed_and_upsert(
                catalog.asset_documents,
                self.suite.asset_vectors,
            )
        self._embed_and_upsert(
            chunk_documents_to_upsert,
            self.suite.chunk_vectors,
        )

        status = self.embedder.status
        self._record_index_states(
            session,
            catalog,
            provider=status.provider,
            model=status.model,
            dimension=status.dimension,
        )
        session.flush()
        keyword_after = {
            name: self.suite.meili_admin.document_count(name) for name in KEYWORD_INDEX_NAMES
        }
        vector_after = {
            self.suite.asset_collection: self.suite.qdrant_admin.point_count(
                self.suite.asset_collection
            ),
            self.suite.chunk_collection: self.suite.qdrant_admin.point_count(
                self.suite.chunk_collection
            ),
        }
        return IndexBuildReport(
            index_version=self.version,
            embedding_provider=status.provider,
            embedding_model=status.model,
            embedding_dimension=status.dimension,
            degraded=status.provider == "local-hashing",
            keyword_counts_before=keyword_before,
            keyword_counts_after=keyword_after,
            vector_counts_before=vector_before,
            vector_counts_after=vector_after,
            keyword_documents_upserted=keyword_upserts,
            vector_points_upserted={
                self.suite.asset_collection: (
                    0 if resume_vectors else len(catalog.asset_documents)
                ),
                self.suite.chunk_collection: len(chunk_documents_to_upsert),
            },
            stale_keyword_documents_removed=removed_keyword,
            stale_vector_points_removed={
                self.suite.asset_collection: len(stale_asset_points),
                self.suite.chunk_collection: len(stale_chunk_points),
            },
        )

    def rebuild_keywords(
        self,
        session: Session,
        *,
        user_id: str = "local",
    ) -> KeywordBuildReport:
        catalog = PostgresIndexCatalog().load(session, user_id=user_id)
        counts_before = {
            name: self.suite.meili_admin.document_count(name)
            for name in KEYWORD_INDEX_NAMES
        }
        expected_ids = {
            name: {document.document_id for document in documents}
            for name, documents in catalog.keyword_documents.items()
        }
        removed, upserted = self._rebuild_keyword_documents(
            catalog,
            expected_ids,
        )
        counts_after = {
            name: self.suite.meili_admin.document_count(name)
            for name in KEYWORD_INDEX_NAMES
        }
        return KeywordBuildReport(
            keyword_counts_before=counts_before,
            keyword_counts_after=counts_after,
            keyword_documents_upserted=upserted,
            stale_keyword_documents_removed=removed,
        )

    def _rebuild_keyword_documents(
        self,
        catalog: IndexCatalog,
        expected_ids: Mapping[str, set[str]],
    ) -> tuple[dict[str, int], dict[str, int]]:
        removed: dict[str, int] = {}
        upserted: dict[str, int] = {}
        queued_transport = getattr(self.suite, "meili_transport", None)
        previous_wait_mode: bool | None = None
        if isinstance(queued_transport, HttpxJSONTransport):
            previous_wait_mode = queued_transport.wait_for_tasks
            queued_transport.wait_for_tasks = False
        try:
            for name, documents in catalog.keyword_documents.items():
                stale_ids = self.suite.meili_admin.document_ids(name) - expected_ids[name]
                if stale_ids:
                    self.suite.keyword_indexes[name].delete(sorted(stale_ids))
                removed[name] = len(stale_ids)
                upserted[name] = len(documents)
                for keyword_batch in _batches(documents, self.write_batch_size):
                    self.suite.keyword_indexes[name].upsert(keyword_batch)
        finally:
            if (
                isinstance(queued_transport, HttpxJSONTransport)
                and previous_wait_mode is not None
            ):
                queued_transport.wait_for_tasks = previous_wait_mode
        if isinstance(queued_transport, HttpxJSONTransport):
            queued_transport.flush_pending_tasks()
        return removed, upserted

    def _embed_and_upsert(
        self,
        documents: Sequence[SearchDocument],
        index: VectorIndex,
    ) -> None:
        """Embed and write bounded batches without retaining the full matrix."""

        for document_batch in _batches(documents, self.vector_write_batch_size):
            points = self._embed(document_batch)
            index.upsert(points)

    def _embed(self, documents: Sequence[SearchDocument]) -> list[VectorPoint]:
        points: list[VectorPoint] = []
        for batch in _batches(documents, self.batch_size):
            texts = [
                f"{document.title}\n{document.text[:MAX_EMBEDDING_TEXT_CHARS]}"
                for document in batch
            ]
            vectors = self.embedder.encode(texts)
            points.extend(
                VectorPoint(
                    point_id=_point_id(document.document_id),
                    document=document,
                    vector=tuple(vector),
                )
                for document, vector in zip(batch, vectors, strict=True)
            )
        return points

    def _record_index_states(
        self,
        session: Session,
        catalog: IndexCatalog,
        *,
        provider: str,
        model: str,
        dimension: int,
    ) -> None:
        current: list[tuple[str, str, UUID, str, str, str | None, int | None]] = []
        for index_name, documents in catalog.keyword_documents.items():
            index_kind = IndexKind.CODE.value if index_name == "code" else IndexKind.KEYWORD.value
            current.extend(
                (
                    index_name,
                    _entity_type(document),
                    _entity_id(document),
                    _document_digest(document),
                    index_kind,
                    "meilisearch",
                    None,
                )
                for document in documents
            )
        current.extend(
            (
                self.suite.asset_collection,
                "asset",
                _entity_id(document),
                _document_digest(document),
                IndexKind.VECTOR.value,
                provider,
                dimension,
            )
            for document in catalog.asset_documents
        )
        current.extend(
            (
                self.suite.chunk_collection,
                "chunk",
                _entity_id(document),
                _document_digest(document),
                IndexKind.VECTOR.value,
                provider,
                dimension,
            )
            for document in catalog.chunk_documents
        )
        index_names = {item[0] for item in current}
        existing = list(
            session.scalars(
                select(IndexState).where(
                    IndexState.version == self.version,
                    IndexState.index_name.in_(index_names),
                )
            ).all()
        )
        by_key = {
            (
                state.index_name,
                state.entity_type,
                state.entity_id,
                state.content_digest,
            ): state
            for state in existing
        }
        for state in existing:
            if state.status == IndexStatus.READY.value:
                state.status = IndexStatus.STALE.value
        indexed_at = datetime.now(UTC)
        for (
            index_name,
            entity_type,
            entity_id,
            digest,
            index_kind,
            state_provider,
            state_dimension,
        ) in current:
            key = (index_name, entity_type, entity_id, digest)
            current_state = by_key.get(key)
            if current_state is None:
                current_state = IndexState(
                    index_name=index_name,
                    index_kind=index_kind,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    content_digest=digest,
                    version=self.version,
                )
                session.add(current_state)
                by_key[key] = current_state
            current_state.status = IndexStatus.READY.value
            current_state.provider = state_provider
            current_state.model = model if index_kind == IndexKind.VECTOR.value else None
            current_state.dimension = state_dimension
            current_state.error = None
            current_state.indexed_at = indexed_at


def _asset_metadata(
    asset: Asset,
    *,
    reading_status: str,
    representation_types: list[str],
    available_representation_types: list[str],
    availability: list[str],
) -> dict[str, Any]:
    available = set(available_representation_types)
    return {
        "domains": list(asset.domain_slugs or []),
        "year": asset.year,
        "asset_type": asset.asset_type,
        "reading_status": reading_status,
        "tags": list(asset.tags or []),
        "authors": list(asset.authors or []),
        "representation_types": representation_types,
        "available_representation_types": available_representation_types,
        "availability": availability,
        "has_pdf": "pdf" in available,
        "has_html": "html" in available,
        "has_code": bool(
            {"source_code", "repository", "notebook"}.intersection(available)
            or asset.asset_type == "repository"
        ),
        "has_guide": bool({"guide", "analysis", "markdown"}.intersection(available)),
    }


def _is_code_chunk(
    chunk_kind: str,
    anchor_kind: str | None,
    representation_type: str,
) -> bool:
    return (
        "code" in chunk_kind.casefold()
        or anchor_kind == "code_lines"
        or representation_type in {"source_code", "repository"}
    )


def _semantic_code_documents(
    documents: Sequence[SearchDocument],
) -> list[SearchDocument]:
    """Bound vectors per repository while keeping every code chunk in Meilisearch."""

    grouped: defaultdict[str, list[SearchDocument]] = defaultdict(list)
    for document in documents:
        grouped[document.asset_id].append(document)
    selected: list[SearchDocument] = []
    for asset_id in sorted(grouped):
        ranked = sorted(
            grouped[asset_id],
            key=lambda document: (
                not bool(
                    isinstance(document.metadata.get("anchor"), Mapping)
                    and document.metadata["anchor"].get("symbol")
                ),
                int(document.metadata.get("chunk_index") or 0),
                document.document_id,
            ),
        )
        selected.extend(ranked[:MAX_VECTOR_CODE_CHUNKS_PER_ASSET])
    return selected


def _anchor_exact_terms(anchor: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not anchor:
        return ()
    return _unique_terms(
        *(
            str(value)
            for key, value in anchor.items()
            if key in {"symbol", "name", "qualified_name"} and value
        )
    )


def _unique_terms(*values: str | None) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = value.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return tuple(result)


def _batches(values: Sequence[T], size: int) -> Iterable[list[T]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


def _point_id(document_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"research-asset-hub:{document_id}"))


def _entity_type(document: SearchDocument) -> str:
    return document.document_id.partition(":")[0]


def _entity_id(document: SearchDocument) -> UUID:
    return UUID(document.document_id.partition(":")[2])


def _document_digest(document: SearchDocument) -> str:
    encoded = json.dumps(
        {
            "document_id": document.document_id,
            "asset_id": document.asset_id,
            "kind": document.kind,
            "title": document.title,
            "text": document.text,
            "metadata": document.metadata,
            "exact_terms": document.exact_terms,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_version(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in value.casefold()
    ).strip("_")
    return normalized or DEFAULT_VECTOR_VERSION


def degraded_hashing_embedder(*, dimension: int = 1024) -> Embedder:
    """Public helper for an explicit no-model build."""

    return HashingEmbedder(dimension=dimension)
