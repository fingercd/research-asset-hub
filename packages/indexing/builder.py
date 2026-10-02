"""Build the four Meilisearch keyword indexes from PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, tuple_, union, update
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
    Source,
    SourceRecord,
    SourceRecordAsset,
)
from packages.domain.serving_scope import active_representation_clause

from .keyword import KeywordSearchService
from .meili import HttpxJSONTransport, KeywordIndex, MeilisearchAdmin, MeilisearchIndex
from .models import SearchDocument

KEYWORD_INDEX_NAMES = ("assets", "documents", "code", "annotations")
DEFAULT_REFRESH_INDEX_NAMES = ("assets", "documents", "annotations")
DEFAULT_INDEX_VERSION = "v1"
DEFAULT_TASK_FLUSH_BATCHES = 16
T = TypeVar("T")
ServingScope = Literal["serving_active", "all"]


@dataclass(frozen=True, slots=True)
class IndexCatalog:
    """Compatibility materialization for small callers and tests.

    Production rebuilds use :meth:`PostgresIndexCatalog.iter_index_batches`
    directly and never instantiate this all-in-memory representation.
    """

    keyword_documents: Mapping[str, tuple[SearchDocument, ...]]


@dataclass(frozen=True, slots=True)
class KeywordBuildReport:
    index_version: str
    keyword_counts_before: Mapping[str, int]
    keyword_counts_after: Mapping[str, int]
    keyword_documents_upserted: Mapping[str, int]
    stale_keyword_documents_removed: Mapping[str, int]
    scope: ServingScope = "serving_active"
    batch_size: int = 256
    peak_buffered_documents: int = 0
    task_flush_batch_count: int = DEFAULT_TASK_FLUSH_BATCHES
    selected_indexes: tuple[str, ...] = KEYWORD_INDEX_NAMES
    skipped_indexes: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    operation: str = "rebuild"
    scoped_keyword_counts_before: Mapping[str, int] | None = None
    scoped_keyword_counts_after: Mapping[str, int] | None = None
    buffering_strategy: str = (
        "uuid_keyset_pages; bounded_meili_task_groups; stale_ids_spooled_to_disk"
    )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RemoteIndexSuite:
    keyword_indexes: dict[str, KeywordIndex]
    meili_admin: MeilisearchAdmin
    meili_transport: HttpxJSONTransport

    @classmethod
    def connect(
        cls,
        *,
        meilisearch_url: str,
        meilisearch_api_key: str | None,
        index_names: Sequence[str] | None = None,
        initialize_indexes: bool = True,
    ) -> RemoteIndexSuite:
        selected_indexes = tuple(
            dict.fromkeys(KEYWORD_INDEX_NAMES if index_names is None else index_names)
        )
        if not selected_indexes:
            raise ValueError("index_names must contain at least one keyword index")
        unknown_indexes = sorted(set(selected_indexes) - set(KEYWORD_INDEX_NAMES))
        if unknown_indexes:
            raise ValueError(f"Unknown keyword indexes: {unknown_indexes}")
        meili_transport = HttpxJSONTransport(
            meilisearch_url,
            api_key=meilisearch_api_key,
            timeout=30,
        )
        meili_admin = MeilisearchAdmin(meili_transport)
        if initialize_indexes:
            for index_name in selected_indexes:
                meili_admin.ensure_index(index_name)
        return cls(
            keyword_indexes={
                index_name: MeilisearchIndex(meili_transport, index_name=index_name)
                for index_name in selected_indexes
            },
            meili_admin=meili_admin,
            meili_transport=meili_transport,
        )

    def close(self) -> None:
        self.meili_transport.close()

    def search_service(self) -> KeywordSearchService:
        return KeywordSearchService(self.keyword_indexes)


@dataclass(frozen=True, slots=True)
class _AssetContext:
    asset: Asset
    metadata: dict[str, Any]
    identifiers: tuple[str, ...]
    representation_digest_by_id: Mapping[UUID, str | None]


class PostgresIndexCatalog:
    """Build backend-neutral documents using deterministic, bounded keyset pages."""

    def load(
        self,
        session: Session,
        *,
        user_id: str = "local",
        scope: ServingScope = "serving_active",
        asset_ids: Iterable[UUID] | None = None,
    ) -> IndexCatalog:
        """Materialize a catalog for compatibility; production uses the iterator."""

        normalized_scope = _normalize_scope(scope)
        selected_asset_ids = None if asset_ids is None else tuple(set(asset_ids))
        return IndexCatalog(
            keyword_documents={
                index_name: tuple(
                    document
                    for batch in self.iter_index_batches(
                        session,
                        index_name=index_name,
                        user_id=user_id,
                        scope=normalized_scope,
                        batch_size=256,
                        asset_ids=selected_asset_ids,
                    )
                    for document in batch
                )
                for index_name in KEYWORD_INDEX_NAMES
            }
        )

    def iter_index_batches(
        self,
        session: Session,
        *,
        index_name: str,
        user_id: str = "local",
        scope: ServingScope = "serving_active",
        batch_size: int = 256,
        asset_ids: Iterable[UUID] | None = None,
    ) -> Iterable[list[SearchDocument]]:
        """Yield one index in UUID order without retaining prior pages."""

        if index_name not in KEYWORD_INDEX_NAMES:
            raise ValueError(f"Unknown keyword index: {index_name}")
        normalized_scope = _normalize_scope(scope)
        selected_asset_ids = None if asset_ids is None else tuple(set(asset_ids))
        if selected_asset_ids == ():
            return
        size = max(1, batch_size)
        if index_name == "assets":
            yield from self._iter_asset_batches(
                session,
                user_id=user_id,
                scope=normalized_scope,
                batch_size=size,
                asset_ids=selected_asset_ids,
            )
        elif index_name in {"documents", "code"}:
            yield from self._iter_chunk_batches(
                session,
                index_name=index_name,
                user_id=user_id,
                scope=normalized_scope,
                batch_size=size,
                asset_ids=selected_asset_ids,
            )
        else:
            yield from self._iter_annotation_batches(
                session,
                user_id=user_id,
                scope=normalized_scope,
                batch_size=size,
                asset_ids=selected_asset_ids,
            )

    def _iter_asset_batches(
        self,
        session: Session,
        *,
        user_id: str,
        scope: ServingScope,
        batch_size: int,
        asset_ids: tuple[UUID, ...] | None,
    ) -> Iterable[list[SearchDocument]]:
        cursor: UUID | None = None
        while True:
            statement = select(Asset).where(Asset.deleted_at.is_(None))
            if asset_ids is not None:
                statement = statement.where(Asset.id.in_(asset_ids))
            if cursor is not None:
                statement = statement.where(Asset.id > cursor)
            assets = list(session.scalars(statement.order_by(Asset.id).limit(batch_size)).all())
            if not assets:
                return
            contexts = self._asset_contexts(
                session,
                {asset.id for asset in assets},
                user_id=user_id,
                scope=scope,
            )
            yield [self._asset_document(contexts[asset.id]) for asset in assets]
            cursor = assets[-1].id

    def _iter_chunk_batches(
        self,
        session: Session,
        *,
        index_name: str,
        user_id: str,
        scope: ServingScope,
        batch_size: int,
        asset_ids: tuple[UUID, ...] | None,
    ) -> Iterable[list[SearchDocument]]:
        cursor: UUID | None = None
        code_clause = func.coalesce(
            or_(
                func.lower(DocumentChunk.chunk_kind).like("%code%"),
                DocumentChunk.anchor_kind == "code_lines",
                Representation.representation_type.in_(("source_code", "repository")),
            ),
            False,
        )
        while True:
            statement = (
                select(
                    DocumentChunk,
                    Representation.representation_type,
                    Representation.sha256,
                    Representation.tree_digest,
                    Representation.revision,
                )
                .join(Representation, Representation.id == DocumentChunk.representation_id)
                .join(Asset, Asset.id == DocumentChunk.asset_id)
                .where(
                    Asset.deleted_at.is_(None),
                    Representation.deleted_at.is_(None),
                    code_clause if index_name == "code" else ~code_clause,
                )
            )
            if scope == "serving_active":
                statement = statement.where(active_representation_clause())
            if asset_ids is not None:
                statement = statement.where(DocumentChunk.asset_id.in_(asset_ids))
            if cursor is not None:
                statement = statement.where(DocumentChunk.id > cursor)
            rows = list(
                session.execute(statement.order_by(DocumentChunk.id).limit(batch_size)).all()
            )
            if not rows:
                return
            contexts = self._asset_contexts(
                session,
                {row[0].asset_id for row in rows},
                user_id=user_id,
                scope=scope,
            )
            documents: list[SearchDocument] = []
            for chunk, rep_type, sha256, tree_digest, revision in rows:
                context = contexts[chunk.asset_id]
                representation_digest = sha256 or tree_digest or revision
                metadata = {
                    **context.metadata,
                    "representation_id": str(chunk.representation_id),
                    "representation_digest": (
                        str(representation_digest) if representation_digest else None
                    ),
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
                documents.append(
                    SearchDocument(
                        document_id=f"chunk:{chunk.id}",
                        asset_id=str(chunk.asset_id),
                        kind=(
                            "code"
                            if _is_code_chunk(chunk.chunk_kind, chunk.anchor_kind, str(rep_type))
                            else "document"
                        ),
                        title=context.asset.title,
                        text=chunk.text_content,
                        metadata=metadata,
                        exact_terms=_anchor_exact_terms(chunk.anchor),
                    )
                )
            yield documents
            cursor = rows[-1][0].id

    def _iter_annotation_batches(
        self,
        session: Session,
        *,
        user_id: str,
        scope: ServingScope,
        batch_size: int,
        asset_ids: tuple[UUID, ...] | None,
    ) -> Iterable[list[SearchDocument]]:
        cursor: UUID | None = None
        active_annotation_representation = exists(
            select(Representation.id).where(
                Representation.id == Annotation.representation_id,
                Representation.deleted_at.is_(None),
                active_representation_clause(),
            )
        )
        while True:
            statement = (
                select(Annotation)
                .join(Asset, Asset.id == Annotation.asset_id)
                .where(Asset.deleted_at.is_(None), Annotation.deleted_at.is_(None))
            )
            if scope == "serving_active":
                statement = statement.where(
                    or_(
                        Annotation.representation_id.is_(None),
                        active_annotation_representation,
                    )
                )
            if asset_ids is not None:
                statement = statement.where(Annotation.asset_id.in_(asset_ids))
            if cursor is not None:
                statement = statement.where(Annotation.id > cursor)
            annotations = list(
                session.scalars(statement.order_by(Annotation.id).limit(batch_size)).all()
            )
            if not annotations:
                return
            contexts = self._asset_contexts(
                session,
                {annotation.asset_id for annotation in annotations},
                user_id=user_id,
                scope=scope,
            )
            documents: list[SearchDocument] = []
            for annotation in annotations:
                context = contexts[annotation.asset_id]
                documents.append(
                    SearchDocument(
                        document_id=f"annotation:{annotation.id}",
                        asset_id=str(annotation.asset_id),
                        kind="annotation",
                        title=annotation.title or context.asset.title,
                        text="\n".join(
                            value
                            for value in (annotation.summary, annotation.body_markdown)
                            if value
                        ),
                        metadata={
                            **context.metadata,
                            "annotation_kind": annotation.kind,
                            "pinned": annotation.pinned,
                            "representation_id": (
                                str(annotation.representation_id)
                                if annotation.representation_id
                                else None
                            ),
                            "representation_digest": (
                                context.representation_digest_by_id.get(
                                    annotation.representation_id
                                )
                                if annotation.representation_id
                                else None
                            ),
                        },
                        exact_terms=_unique_terms(annotation.title),
                    )
                )
            yield documents
            cursor = annotations[-1].id

    def _asset_contexts(
        self,
        session: Session,
        asset_ids: set[UUID],
        *,
        user_id: str,
        scope: ServingScope,
    ) -> dict[UUID, _AssetContext]:
        if not asset_ids:
            return {}
        assets = list(
            session.scalars(
                select(Asset).where(Asset.id.in_(asset_ids), Asset.deleted_at.is_(None))
            ).all()
        )
        identifiers: defaultdict[UUID, list[str]] = defaultdict(list)
        for asset_id, value, canonical in session.execute(
            select(
                ExternalIdentifierModel.asset_id,
                ExternalIdentifierModel.value,
                ExternalIdentifierModel.canonical,
            ).where(ExternalIdentifierModel.asset_id.in_(asset_ids))
        ):
            identifiers[asset_id].extend((str(canonical), str(value)))
        reading_status = {
            asset_id: str(status)
            for asset_id, status in session.execute(
                select(ReadingState.asset_id, ReadingState.status).where(
                    ReadingState.user_id == user_id,
                    ReadingState.asset_id.in_(asset_ids),
                )
            )
        }
        representation_types: defaultdict[UUID, set[str]] = defaultdict(set)
        available_representation_types: defaultdict[UUID, set[str]] = defaultdict(set)
        representation_availability: defaultdict[UUID, set[str]] = defaultdict(set)
        representation_digest_by_asset: defaultdict[UUID, dict[UUID, str | None]] = defaultdict(
            dict
        )
        representation_statement = select(
            Representation.id,
            Representation.asset_id,
            Representation.representation_type,
            Representation.availability,
            Representation.sha256,
            Representation.tree_digest,
            Representation.revision,
        ).where(
            Representation.deleted_at.is_(None),
            Representation.asset_id.in_(asset_ids),
        )
        if scope == "serving_active":
            representation_statement = representation_statement.where(
                active_representation_clause()
            )
        for (
            representation_id,
            asset_id,
            rep_type,
            availability,
            sha256,
            tree_digest,
            revision,
        ) in session.execute(representation_statement):
            normalized_type = str(rep_type)
            normalized_availability = str(availability)
            representation_types[asset_id].add(normalized_type)
            representation_availability[asset_id].add(normalized_availability)
            if normalized_availability == "available":
                available_representation_types[asset_id].add(normalized_type)
            digest = sha256 or tree_digest or revision
            representation_digest_by_asset[asset_id][representation_id] = (
                str(digest) if digest else None
            )
        contexts: dict[UUID, _AssetContext] = {}
        for asset in assets:
            contexts[asset.id] = _AssetContext(
                asset=asset,
                metadata=_asset_metadata(
                    asset,
                    reading_status=reading_status.get(asset.id, ReadingStatus.UNREAD.value),
                    representation_types=sorted(representation_types[asset.id]),
                    available_representation_types=sorted(available_representation_types[asset.id]),
                    availability=sorted(representation_availability[asset.id]),
                ),
                identifiers=tuple(identifiers[asset.id]),
                representation_digest_by_id=representation_digest_by_asset[asset.id],
            )
        return contexts

    @staticmethod
    def _asset_document(context: _AssetContext) -> SearchDocument:
        asset = context.asset
        return SearchDocument(
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
            metadata=context.metadata,
            exact_terms=_unique_terms(
                asset.title,
                asset.title_zh,
                *list(asset.authors or []),
                *list(asset.source_urls or []),
                *context.identifiers,
            ),
        )


class IndexBuilder:
    def __init__(
        self,
        suite: RemoteIndexSuite,
        *,
        version: str = DEFAULT_INDEX_VERSION,
        write_batch_size: int = 256,
        task_flush_batch_count: int = DEFAULT_TASK_FLUSH_BATCHES,
        catalog: PostgresIndexCatalog | None = None,
    ) -> None:
        self.suite = suite
        self.version = _safe_version(version)
        self.write_batch_size = max(1, write_batch_size)
        self.task_flush_batch_count = max(1, task_flush_batch_count)
        self.catalog = catalog or PostgresIndexCatalog()

    def refresh_keywords(
        self,
        session: Session,
        *,
        asset_ids: Iterable[UUID],
        user_id: str = "local",
        scope: ServingScope = "serving_active",
        index_names: Sequence[str] = DEFAULT_REFRESH_INDEX_NAMES,
    ) -> KeywordBuildReport:
        """Reconcile only these assets, including remote chunks already deleted locally.

        All remote reads carry an asset filter. Old logical IDs are spooled before
        writes so offset pagination cannot drift, and index states use the same
        projection, digest and version as the full builder.
        """

        selected_assets = tuple(sorted(set(asset_ids), key=str))
        if not selected_assets:
            raise ValueError("asset_ids must contain at least one asset UUID")
        selected_indexes = tuple(dict.fromkeys(index_names))
        if not selected_indexes or set(selected_indexes) - set(KEYWORD_INDEX_NAMES):
            raise ValueError("index_names must contain known keyword indexes")
        normalized_scope = _normalize_scope(scope)
        _require_repeatable_read_snapshot(session)
        counts_before = {
            name: self.suite.meili_admin.document_count(name) for name in selected_indexes
        }
        upserted = dict.fromkeys(selected_indexes, 0)
        removed = dict.fromkeys(selected_indexes, 0)
        scoped_before = dict.fromkeys(selected_indexes, 0)
        scoped_after = dict.fromkeys(selected_indexes, 0)
        peak_buffered_documents = 0
        indexed_at = datetime.now(UTC)
        previous_wait_mode = self.suite.meili_transport.wait_for_tasks
        self.suite.meili_transport.wait_for_tasks = False
        try:
            with session.begin_nested():
                self._mark_previous_states_stale(
                    session,
                    indexed_at=indexed_at,
                    index_names=selected_indexes,
                    asset_ids=selected_assets,
                )
                for index_name in selected_indexes:
                    with tempfile.TemporaryFile(
                        mode="w+t", encoding="utf-8", newline="\n"
                    ) as spool:
                        for old_ids in self.suite.meili_admin.iter_asset_document_id_batches(
                            index_name, asset_ids=selected_assets, batch_size=self.write_batch_size
                        ):
                            scoped_before[index_name] += len(old_ids)
                            self._mark_remote_states_stale(
                                session,
                                index_name=index_name,
                                document_ids=old_ids,
                                indexed_at=indexed_at,
                            )
                            for document_id in old_ids:
                                spool.write(json.dumps(document_id) + "\n")
                        queued_batches = 0
                        for batch in self.catalog.iter_index_batches(
                            session,
                            index_name=index_name,
                            user_id=user_id,
                            scope=normalized_scope,
                            batch_size=self.write_batch_size,
                            asset_ids=selected_assets,
                        ):
                            peak_buffered_documents = max(peak_buffered_documents, len(batch))
                            self.suite.keyword_indexes[index_name].upsert(batch)
                            self._record_index_state_batch(
                                session,
                                index_name=index_name,
                                documents=batch,
                                indexed_at=indexed_at,
                            )
                            upserted[index_name] += len(batch)
                            queued_batches += 1
                            if queued_batches >= self.task_flush_batch_count:
                                self.suite.meili_transport.flush_pending_tasks()
                                queued_batches = 0
                        self.suite.meili_transport.flush_pending_tasks()
                        spool.seek(0)
                        queued_batches = 0
                        while old_ids := [
                            json.loads(line) for line in _take_lines(spool, self.write_batch_size)
                        ]:
                            current_ids = self._current_document_ids(
                                session, index_name=index_name, document_ids=old_ids
                            )
                            stale_ids = [value for value in old_ids if value not in current_ids]
                            if stale_ids:
                                self.suite.keyword_indexes[index_name].delete(stale_ids)
                                removed[index_name] += len(stale_ids)
                                queued_batches += 1
                                if queued_batches >= self.task_flush_batch_count:
                                    self.suite.meili_transport.flush_pending_tasks()
                                    queued_batches = 0
                        self.suite.meili_transport.flush_pending_tasks()
                    scoped_after[index_name] = sum(
                        len(batch)
                        for batch in self.suite.meili_admin.iter_asset_document_id_batches(
                            index_name, asset_ids=selected_assets, batch_size=self.write_batch_size
                        )
                    )
                    if scoped_after[index_name] != upserted[index_name]:
                        raise RuntimeError(
                            f"Index {index_name!r} scoped count mismatch: "
                            f"expected {upserted[index_name]}, got {scoped_after[index_name]}"
                        )
                session.flush()
                counts_after = {
                    name: self.suite.meili_admin.document_count(name) for name in selected_indexes
                }
        finally:
            self.suite.meili_transport.wait_for_tasks = previous_wait_mode
        return KeywordBuildReport(
            index_version=self.version,
            keyword_counts_before=counts_before,
            keyword_counts_after=counts_after,
            keyword_documents_upserted=upserted,
            stale_keyword_documents_removed=removed,
            scope=normalized_scope,
            batch_size=self.write_batch_size,
            peak_buffered_documents=peak_buffered_documents,
            task_flush_batch_count=self.task_flush_batch_count,
            selected_indexes=selected_indexes,
            skipped_indexes=tuple(
                name for name in KEYWORD_INDEX_NAMES if name not in selected_indexes
            ),
            asset_ids=tuple(str(value) for value in selected_assets),
            operation="refresh",
            scoped_keyword_counts_before=scoped_before,
            scoped_keyword_counts_after=scoped_after,
        )

    def rebuild_keywords(
        self,
        session: Session,
        *,
        user_id: str = "local",
        scope: ServingScope = "serving_active",
        index_names: Sequence[str] | None = None,
    ) -> KeywordBuildReport:
        """Rebuild in bounded pages; a failed remote task never marks DB state ready."""

        normalized_scope = _normalize_scope(scope)
        selected_indexes = tuple(
            dict.fromkeys(KEYWORD_INDEX_NAMES if index_names is None else index_names)
        )
        if not selected_indexes:
            raise ValueError("index_names must contain at least one keyword index")
        unknown_indexes = sorted(set(selected_indexes) - set(KEYWORD_INDEX_NAMES))
        if unknown_indexes:
            raise ValueError(f"Unknown keyword indexes: {unknown_indexes}")
        skipped_indexes = tuple(
            name for name in KEYWORD_INDEX_NAMES if name not in selected_indexes
        )
        _require_repeatable_read_snapshot(session)
        counts_before = {
            name: self.suite.meili_admin.document_count(name) for name in KEYWORD_INDEX_NAMES
        }
        removed = dict.fromkeys(KEYWORD_INDEX_NAMES, 0)
        upserted = dict.fromkeys(KEYWORD_INDEX_NAMES, 0)
        peak_buffered_documents = 0
        indexed_at = datetime.now(UTC)
        previous_wait_mode = self.suite.meili_transport.wait_for_tasks
        self.suite.meili_transport.wait_for_tasks = False
        try:
            # A savepoint keeps index-state/version changes atomic even if a caller
            # catches a Meilisearch failure and later commits its outer transaction.
            with session.begin_nested():
                self._mark_previous_states_stale(
                    session,
                    indexed_at=indexed_at,
                    index_names=selected_indexes,
                )
                for index_name in selected_indexes:
                    queued_batches = 0
                    for batch in self.catalog.iter_index_batches(
                        session,
                        index_name=index_name,
                        user_id=user_id,
                        scope=normalized_scope,
                        batch_size=self.write_batch_size,
                    ):
                        peak_buffered_documents = max(peak_buffered_documents, len(batch))
                        self.suite.keyword_indexes[index_name].upsert(batch)
                        self._record_index_state_batch(
                            session,
                            index_name=index_name,
                            documents=batch,
                            indexed_at=indexed_at,
                        )
                        upserted[index_name] += len(batch)
                        queued_batches += 1
                        if queued_batches >= self.task_flush_batch_count:
                            self.suite.meili_transport.flush_pending_tasks()
                            queued_batches = 0
                    # Additions must be durable before stale documents are inspected.
                    self.suite.meili_transport.flush_pending_tasks()
                    removed[index_name] = self._delete_stale_documents(
                        session, index_name=index_name
                    )
                self.suite.meili_transport.flush_pending_tasks()
                session.flush()
                counts_after = {
                    name: self.suite.meili_admin.document_count(name)
                    for name in KEYWORD_INDEX_NAMES
                }
                for name in selected_indexes:
                    if counts_after[name] != upserted[name]:
                        raise RuntimeError(
                            f"Index {name!r} count mismatch after rebuild: "
                            f"expected {upserted[name]}, got {counts_after[name]}"
                        )
        finally:
            self.suite.meili_transport.wait_for_tasks = previous_wait_mode
        return KeywordBuildReport(
            index_version=self.version,
            keyword_counts_before=counts_before,
            keyword_counts_after=counts_after,
            keyword_documents_upserted=upserted,
            stale_keyword_documents_removed=removed,
            scope=normalized_scope,
            batch_size=self.write_batch_size,
            peak_buffered_documents=peak_buffered_documents,
            task_flush_batch_count=self.task_flush_batch_count,
            selected_indexes=selected_indexes,
            skipped_indexes=skipped_indexes,
        )

    def _mark_previous_states_stale(
        self,
        session: Session,
        *,
        indexed_at: datetime,
        index_names: Sequence[str] = KEYWORD_INDEX_NAMES,
        asset_ids: Sequence[UUID] | None = None,
    ) -> None:
        statement = update(IndexState)
        if asset_ids is not None:
            statement = statement.where(
                or_(
                    and_(IndexState.entity_type == "asset", IndexState.entity_id.in_(asset_ids)),
                    and_(
                        IndexState.entity_type == "chunk",
                        IndexState.entity_id.in_(
                            select(DocumentChunk.id).where(DocumentChunk.asset_id.in_(asset_ids))
                        ),
                    ),
                    and_(
                        IndexState.entity_type == "annotation",
                        IndexState.entity_id.in_(
                            select(Annotation.id).where(Annotation.asset_id.in_(asset_ids))
                        ),
                    ),
                )
            )
        session.execute(
            statement.where(
                IndexState.version == self.version,
                IndexState.index_name.in_(tuple(index_names)),
                IndexState.status == IndexStatus.READY.value,
            ).values(
                status=IndexStatus.STALE.value,
                updated_at=indexed_at,
                row_version=IndexState.row_version + 1,
            )
        )

    def _mark_remote_states_stale(
        self,
        session: Session,
        *,
        index_name: str,
        document_ids: Sequence[str],
        indexed_at: datetime,
    ) -> None:
        pairs = _logical_document_pairs(document_ids)
        if pairs:
            session.execute(
                update(IndexState)
                .where(
                    IndexState.version == self.version,
                    IndexState.index_name == index_name,
                    IndexState.status == IndexStatus.READY.value,
                    tuple_(IndexState.entity_type, IndexState.entity_id).in_(pairs),
                )
                .values(
                    status=IndexStatus.STALE.value,
                    updated_at=indexed_at,
                    row_version=IndexState.row_version + 1,
                )
            )

    def _record_index_state_batch(
        self,
        session: Session,
        *,
        index_name: str,
        documents: Sequence[SearchDocument],
        indexed_at: datetime,
    ) -> None:
        if not documents:
            return
        current = [
            (_entity_type(document), _entity_id(document), _document_digest(document))
            for document in documents
        ]
        existing = list(
            session.scalars(
                select(IndexState).where(
                    IndexState.version == self.version,
                    IndexState.index_name == index_name,
                    tuple_(
                        IndexState.entity_type,
                        IndexState.entity_id,
                        IndexState.content_digest,
                    ).in_(set(current)),
                )
            ).all()
        )
        by_key = {
            (state.entity_type, state.entity_id, state.content_digest): state for state in existing
        }
        index_kind = IndexKind.CODE.value if index_name == "code" else IndexKind.KEYWORD.value
        for entity_type, entity_id, digest in current:
            state = by_key.get((entity_type, entity_id, digest))
            if state is None:
                state = IndexState(
                    index_name=index_name,
                    index_kind=index_kind,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    content_digest=digest,
                    version=self.version,
                )
                session.add(state)
            state.status = IndexStatus.READY.value
            state.provider = "meilisearch"
            state.model = None
            state.dimension = None
            state.error = None
            state.indexed_at = indexed_at
        session.flush()

    def _delete_stale_documents(self, session: Session, *, index_name: str) -> int:
        """Spool stale logical IDs, then delete them without pagination drift."""

        removed = 0
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", newline="\n") as spool:
            for remote_ids in self._iter_remote_document_id_batches(index_name):
                current_ids = self._current_document_ids(
                    session, index_name=index_name, document_ids=remote_ids
                )
                for document_id in remote_ids:
                    if document_id not in current_ids:
                        spool.write(json.dumps(document_id, ensure_ascii=False) + "\n")
                        removed += 1
            spool.seek(0)
            queued_batches = 0
            while True:
                stale_batch = [
                    json.loads(line) for line in _take_lines(spool, self.write_batch_size)
                ]
                if not stale_batch:
                    break
                self.suite.keyword_indexes[index_name].delete(stale_batch)
                queued_batches += 1
                if queued_batches >= self.task_flush_batch_count:
                    self.suite.meili_transport.flush_pending_tasks()
                    queued_batches = 0
            self.suite.meili_transport.flush_pending_tasks()
        return removed

    def _iter_remote_document_id_batches(self, index_name: str) -> Iterable[list[str]]:
        """Read stable remote pages before any delete mutates their offsets."""

        transport = getattr(self.suite.meili_admin, "transport", None)
        if transport is None:
            # Compatibility for the tiny in-memory admin used by unit tests.
            values = sorted(self.suite.meili_admin.document_ids(index_name))
            yield from _batches(values, self.write_batch_size)
            return
        offset = 0
        while True:
            response = transport.request(
                "GET",
                f"/indexes/{index_name}/documents?fields=logical_document_id"
                f"&limit={self.write_batch_size}&offset={offset}",
            )
            results = response.get("results")
            if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
                raise TypeError("Expected a Meilisearch document result sequence")
            values = [
                str(item["logical_document_id"])
                for item in results
                if isinstance(item, Mapping) and item.get("logical_document_id") is not None
            ]
            if len(values) != len(results):
                raise RuntimeError(
                    f"Index {index_name!r} contains a document without logical_document_id; "
                    "stale reconciliation cannot safely address it"
                )
            if values:
                yield values
            if len(results) < self.write_batch_size:
                return
            offset += len(results)

    def _current_document_ids(
        self,
        session: Session,
        *,
        index_name: str,
        document_ids: Sequence[str],
    ) -> set[str]:
        canonical_by_pair = _logical_document_pairs(document_ids)
        if not canonical_by_pair:
            return set()
        ready_pairs = set(
            session.execute(
                select(IndexState.entity_type, IndexState.entity_id).where(
                    IndexState.version == self.version,
                    IndexState.index_name == index_name,
                    IndexState.status == IndexStatus.READY.value,
                    tuple_(IndexState.entity_type, IndexState.entity_id).in_(
                        set(canonical_by_pair)
                    ),
                )
            ).all()
        )
        return {
            document_id for pair, document_id in canonical_by_pair.items() if pair in ready_pairs
        }


def resolve_asset_scope(
    session: Session,
    *,
    asset_ids: Iterable[UUID] | None = None,
    source_alias: str | None = None,
) -> tuple[UUID, ...] | None:
    """Resolve a source's primary and materialized assets; combine filters by intersection.

    Historical/deleted assets remain in scope so refresh can remove their remote
    documents. ``None`` means no restriction; an empty tuple means no matching assets.
    """

    selected = None if asset_ids is None else set(asset_ids)
    if source_alias is not None:
        source_id = session.scalar(select(Source.id).where(Source.alias == source_alias))
        if source_id is None:
            raise ValueError(f"Unknown source alias: {source_alias!r}.")
        source_assets: set[UUID] = set(
            session.scalars(
                union(
                    select(SourceRecord.asset_id).where(
                        SourceRecord.source_id == source_id, SourceRecord.asset_id.is_not(None)
                    ),
                    select(SourceRecordAsset.asset_id)
                    .join(SourceRecord, SourceRecord.id == SourceRecordAsset.source_record_id)
                    .where(SourceRecord.source_id == source_id),
                )
            ).all()
        )
        selected = source_assets if selected is None else selected.intersection(source_assets)
    return None if selected is None else tuple(sorted(selected, key=str))


def _logical_document_pairs(document_ids: Sequence[str]) -> dict[tuple[str, UUID], str]:
    pairs: dict[tuple[str, UUID], str] = {}
    for document_id in document_ids:
        entity_type, separator, raw_id = document_id.partition(":")
        if not separator:
            continue
        try:
            entity_id = UUID(raw_id)
        except ValueError:
            continue
        if f"{entity_type}:{entity_id}" == document_id:
            pairs[(entity_type, entity_id)] = document_id
    return pairs


def _require_repeatable_read_snapshot(session: Session) -> None:
    """Require one PostgreSQL snapshot and serialize concurrent rebuilds."""

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    connection = session.connection()
    isolation = connection.get_isolation_level().replace("_", " ").upper()
    if isolation not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise RuntimeError(
            "Streaming index builds require PostgreSQL REPEATABLE READ or SERIALIZABLE "
            f"isolation, got {isolation!r}"
        )
    # Prevent two builders from interleaving writes from different snapshots.
    session.execute(select(func.pg_advisory_xact_lock(1_384_425_732)))


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


def _is_code_chunk(chunk_kind: str, anchor_kind: str | None, representation_type: str) -> bool:
    return (
        "code" in chunk_kind.casefold()
        or anchor_kind == "code_lines"
        or representation_type in {"source_code", "repository"}
    )


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


def _take_lines(stream: Any, size: int) -> Iterable[str]:
    for _ in range(size):
        line = stream.readline()
        if not line:
            return
        yield line


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
    return normalized or DEFAULT_INDEX_VERSION


def _normalize_scope(value: str) -> ServingScope:
    if value == "serving_active":
        return "serving_active"
    if value == "all":
        return "all"
    raise ValueError("scope must be 'serving_active' or 'all'")
