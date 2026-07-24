from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from sqlalchemy import delete, select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.enums import (
    ActivityStatus,
    AnchorStatus,
    IndexKind,
    IndexStatus,
    JobStatus,
    OutboxStatus,
)
from packages.domain.models import (
    Activity,
    Annotation,
    Asset,
    DocumentChunk,
    IndexState,
    Job,
    OutboxEvent,
    Representation,
    Source,
)
from packages.extraction import ExtractionPipeline, ExtractionStatus
from packages.indexing import MeilisearchIndex, SearchDocument

JobHandler = Callable[[dict[str, object]], dict[str, object]]
_ANCHOR_KIND = {
    "pdf": "pdf_page",
    "html": "html_heading",
    "line": "code_lines",
    "notebook": "notebook_cell",
    "tabular": "text_lines",
}
_CHUNK_WRITE_BATCH_SIZE = 100
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "vendor",
    }
)


class _MeiliWriteTransport:
    """Adapt the generic index contract to Meilisearch's array write payloads."""

    def __init__(self, settings: ApiSettings) -> None:
        self.client = httpx.Client(
            base_url=settings.meilisearch_url,
            headers={"Authorization": f"Bearer {settings.meilisearch_api_key}"},
            timeout=10,
        )
        self._known_indexes: set[str] = set()

    def close(self) -> None:
        self.client.close()

    def _wait_for_task(self, document: Mapping[str, Any]) -> None:
        task_uid = document.get("taskUid")
        if task_uid is None:
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = self.client.get(f"/tasks/{task_uid}")
            response.raise_for_status()
            task = response.json()
            status = task.get("status") if isinstance(task, dict) else None
            if status == "succeeded":
                return
            if status in {"failed", "canceled"}:
                raise RuntimeError(f"Meilisearch task {task_uid} ended with {status}.")
            time.sleep(0.05)
        raise TimeoutError(f"Meilisearch task {task_uid} did not finish within 30 seconds.")

    def _ensure_index(self, index_name: str) -> None:
        if index_name in self._known_indexes:
            return
        response = self.client.get(f"/indexes/{index_name}")
        if response.status_code == 404:
            created = self.client.post(
                "/indexes",
                json={"uid": index_name, "primaryKey": "document_id"},
            )
            created.raise_for_status()
            value = created.json()
            if isinstance(value, dict):
                self._wait_for_task(value)
        else:
            response.raise_for_status()
        self._known_indexes.add(index_name)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "indexes":
            self._ensure_index(parts[1])
        body: Any = dict(payload or {})
        if path.endswith("/documents") and isinstance(body.get("documents"), list):
            body = body["documents"]
        elif path.endswith("/documents/delete-batch") and isinstance(body.get("ids"), list):
            body = body["ids"]
        response = self.client.request(method, path, json=body if payload is not None else None)
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, Mapping):
            raise TypeError("Expected a JSON object response from Meilisearch.")
        self._wait_for_task(raw)
        return raw


def _uuid_parameter(parameters: dict[str, object], name: str) -> UUID:
    try:
        return UUID(str(parameters[name]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Job parameter {name!r} must be a UUID.") from error


def _chunk_uuid(chunk_identity: str, source_path: str) -> UUID:
    """Map a stable extractor identity and its bundle path to a database UUID."""

    return uuid5(
        NAMESPACE_URL,
        f"research-asset-hub:chunk:{source_path}:{chunk_identity}",
    )


class RuntimeHandlers:
    def __init__(
        self,
        database: DatabaseManager,
        settings: ApiSettings,
        registry: SourceRegistry,
        *,
        extraction_pipeline: ExtractionPipeline | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.registry = registry
        self.extraction_pipeline = extraction_pipeline or ExtractionPipeline(
            enable_tree_sitter=settings.worker_tree_sitter_enabled
        )

    def dispatch(
        self,
        job_type: str,
        parameters: dict[str, object],
        *,
        job_id: UUID | None = None,
    ) -> dict[str, object]:
        handlers: dict[str, JobHandler] = {
            "extract_representation": self.extract_representation,
            "representation.extract": self.extract_representation,
            "index_representation": self.index_representation,
            "representation.index": self.index_representation,
            "index_asset": self.index_asset,
            "asset.index": self.index_asset,
            "delete_asset_index": self.delete_asset_index,
            "asset.index-delete": self.delete_asset_index,
        }
        try:
            handler = handlers[job_type]
        except KeyError as error:
            raise ValueError(f"Unsupported job type: {job_type}") from error
        runtime_parameters = dict(parameters)
        if job_id is not None:
            runtime_parameters["_job_id"] = str(job_id)
        return handler(runtime_parameters)

    def _representation_paths(
        self,
        representation_id: UUID,
    ) -> tuple[Representation, Path, list[Path]]:
        with self.database.session() as session:
            representation = session.get(Representation, representation_id)
            if (
                representation is None
                or representation.deleted_at is not None
                or representation.source_id is None
                or representation.path_ref is None
            ):
                raise FileNotFoundError(
                    f"Representation {representation_id} has no local registered file."
                )
            source = session.get(Source, representation.source_id)
            if source is None or not source.enabled:
                raise FileNotFoundError("Representation source is unavailable.")
            registered_path = self.registry.resolve(
                source.alias,
                representation.root_key,
                representation.path_ref,
            )
            session.expunge(representation)
        if registered_path.is_file():
            return representation, registered_path.parent, [registered_path]
        if not registered_path.is_dir():
            raise FileNotFoundError(
                f"Registered representation path does not exist: {registered_path}"
            )
        candidates: list[Path] = []
        for current_root, directory_names, file_names in os.walk(
            registered_path,
            followlinks=False,
        ):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in _IGNORED_DIRECTORY_NAMES
                and not (Path(current_root) / name).is_symlink()
            )
            for file_name in sorted(file_names):
                candidate = (Path(current_root) / file_name).resolve(strict=False)
                try:
                    candidate.relative_to(registered_path)
                    size = candidate.stat().st_size
                except (ValueError, OSError):
                    continue
                if (
                    size > self.settings.worker_max_file_bytes
                    or self.extraction_pipeline.select(candidate) is None
                ):
                    continue
                candidates.append(candidate)
                if len(candidates) >= self.settings.worker_max_files_per_representation:
                    break
            if len(candidates) >= self.settings.worker_max_files_per_representation:
                break
        if not candidates:
            raise FileNotFoundError(
                f"No supported files exist in registered directory {registered_path}."
            )
        candidates.sort(key=lambda item: item.relative_to(registered_path).as_posix())
        return representation, registered_path, candidates

    def extract_representation(
        self,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        representation_id = _uuid_parameter(parameters, "representation_id")
        representation, base_path, paths = self._representation_paths(representation_id)
        with self.database.session() as session:
            activity = Activity(
                job_id=(
                    UUID(str(parameters["_job_id"]))
                    if parameters.get("_job_id") is not None
                    else None
                ),
                asset_id=representation.asset_id,
                representation_id=representation.id,
                activity_type="extraction",
                tool_name="extraction-pipeline",
                tool_version="1",
                status=ActivityStatus.RUNNING.value,
                parameters={
                    "file_count": len(paths),
                    "materialization_kind": representation.materialization_kind,
                },
            )
            session.add(activity)
            session.flush()
            activity_id = activity.id
        try:
            results = []
            warnings: list[str] = []
            extracted_chunk_count = 0
            for path in paths:
                try:
                    result = self.extraction_pipeline.extract(
                        path,
                        representation_id=str(representation.id),
                    )
                    remaining = (
                        self.settings.worker_max_chunks_per_representation - extracted_chunk_count
                    )
                    if remaining <= 0:
                        warnings.append(
                            "Representation chunk cap reached; remaining files "
                            "were intentionally skipped."
                        )
                        break
                    if len(result.chunks) > remaining:
                        result = replace(
                            result,
                            chunks=result.chunks[:remaining],
                            status=ExtractionStatus.PARTIAL,
                            warnings=(
                                *result.warnings,
                                "Representation chunk cap truncated this file.",
                            ),
                        )
                    results.append((path, result))
                    extracted_chunk_count += len(result.chunks)
                    if extracted_chunk_count >= self.settings.worker_max_chunks_per_representation:
                        warnings.append(
                            "Representation chunk cap reached; remaining files "
                            "were intentionally skipped."
                        )
                        break
                except Exception as error:
                    relative = path.relative_to(base_path).as_posix()
                    warnings.append(f"{relative}: {type(error).__name__}: extraction failed.")
            successful = [
                result
                for _, result in results
                if result.status in {ExtractionStatus.SUCCEEDED, ExtractionStatus.PARTIAL}
            ]
            if not successful:
                raise RuntimeError("No registered file could be extracted successfully.")
            digest_builder = hashlib.sha256()
            for path, result in results:
                digest_builder.update(path.relative_to(base_path).as_posix().encode())
                digest_builder.update(result.representation_hash.encode())
                warnings.extend(result.warnings)
            representation_hash = digest_builder.hexdigest()
            status = (
                ExtractionStatus.SUCCEEDED
                if len(successful) == len(results) and not warnings
                else ExtractionStatus.PARTIAL
            )
            with self.database.session() as session:
                stored_activity = session.get(Activity, activity_id)
                if stored_activity is None:
                    raise RuntimeError("Extraction activity disappeared.")
                previous_digest = session.scalar(
                    select(Activity.output_digest)
                    .where(
                        Activity.representation_id == representation.id,
                        Activity.activity_type == "extraction",
                        Activity.status == ActivityStatus.SUCCEEDED.value,
                        Activity.id != stored_activity.id,
                        Activity.output_digest.is_not(None),
                    )
                    .order_by(Activity.finished_at.desc())
                    .limit(1)
                )
                prepared_chunks: list[dict[str, Any]] = []
                chunk_index = 0
                for path, result in results:
                    source_path = path.relative_to(base_path).as_posix()
                    for chunk in result.chunks:
                        anchor = dict(chunk.anchor)
                        anchor["source_path"] = source_path
                        if anchor.get("kind") == "line":
                            anchor["path"] = source_path
                            if representation.revision:
                                anchor["revision"] = representation.revision
                        anchor_kind = _ANCHOR_KIND.get(str(anchor.get("kind")))
                        prepared_chunks.append(
                            {
                                "id": _chunk_uuid(chunk.chunk_id, source_path),
                                "asset_id": representation.asset_id,
                                "representation_id": representation.id,
                                "activity_id": stored_activity.id,
                                "chunk_index": chunk_index,
                                "chunk_kind": representation.representation_type,
                                "text_content": chunk.text,
                                "content_hash": chunk.content_hash,
                                "parser_name": chunk.extractor,
                                "parser_version": chunk.extractor_version,
                                "language": (
                                    str(chunk.metadata["language"])
                                    if chunk.metadata.get("language")
                                    else None
                                ),
                                "anchor_kind": anchor_kind,
                                "anchor": anchor,
                                "token_count": len(chunk.text.split()),
                            }
                        )
                        chunk_index += 1
                existing_chunks = list(
                    session.scalars(
                        select(DocumentChunk).where(
                            DocumentChunk.representation_id == representation.id
                        )
                    ).all()
                )
                if existing_chunks:
                    # Stable chunk IDs can move to a different global chunk index
                    # when a newly discovered file sorts ahead of existing files.
                    # Stage every old row in a disjoint index range before the
                    # upsert so PostgreSQL never observes a transient collision on
                    # (representation, parser, parser_version, chunk_index).
                    staging_offset = (
                        max(
                            max(item.chunk_index for item in existing_chunks),
                            len(prepared_chunks) - 1,
                        )
                        + 1
                    )
                    for staged_chunk in existing_chunks:
                        staged_chunk.chunk_index += staging_offset
                    session.flush()
                existing_by_id = {item.id: item for item in existing_chunks}
                current_ids = {UUID(str(item["id"])) for item in prepared_chunks}
                stale_ids = {item.id for item in existing_chunks} - current_ids
                if stale_ids:
                    session.execute(delete(DocumentChunk).where(DocumentChunk.id.in_(stale_ids)))
                for values in prepared_chunks:
                    chunk_id = UUID(str(values["id"]))
                    existing_chunk = existing_by_id.get(chunk_id)
                    if existing_chunk is None:
                        session.add(DocumentChunk(**values))
                    else:
                        for field_name, value in values.items():
                            if field_name != "id":
                                setattr(existing_chunk, field_name, value)
                    if (int(values["chunk_index"]) + 1) % _CHUNK_WRITE_BATCH_SIZE == 0:
                        # Large repositories can reach the configured 20k chunk
                        # ceiling. Flush bounded groups so the Windows-to-WSL
                        # PostgreSQL connection never receives one oversized
                        # extended-protocol message.
                        session.flush()
                if previous_digest is not None and previous_digest != representation_hash:
                    anchored_annotations = session.scalars(
                        select(Annotation).where(
                            Annotation.representation_id == representation.id,
                            Annotation.deleted_at.is_(None),
                            Annotation.anchor_status == AnchorStatus.CURRENT.value,
                        )
                    ).all()
                    for annotation in anchored_annotations:
                        annotation.anchor_status = AnchorStatus.STALE.value
                stored_activity.status = ActivityStatus.SUCCEEDED.value
                stored_activity.output_digest = representation_hash
                stored_activity.result = {
                    "status": status.value,
                    "file_count": len(results),
                    "chunk_count": chunk_index,
                    "warnings": warnings,
                    "ocr_states": sorted({result.ocr_state.value for result in successful}),
                }
                stored_activity.finished_at = datetime.now(UTC)
                dedupe_key = f"representation:{representation.id}:{representation_hash}:index"
                existing = session.scalar(
                    select(Job).where(
                        Job.job_type == "index_representation",
                        Job.dedupe_key == dedupe_key,
                    )
                )
                if existing is None:
                    session.add(
                        Job(
                            job_type="index_representation",
                            parameters={
                                "representation_id": str(representation.id),
                            },
                            status=JobStatus.PENDING.value,
                            dedupe_key=dedupe_key,
                        )
                    )
            return {
                "status": status.value,
                "file_count": len(results),
                "chunk_count": sum(len(result.chunks) for _, result in results),
                "representation_hash": representation_hash,
            }
        except Exception as error:
            with self.database.session() as session:
                failed_activity = session.get(Activity, activity_id)
                if failed_activity is not None:
                    failed_activity.status = ActivityStatus.FAILED.value
                    failed_activity.finished_at = datetime.now(UTC)
                    failed_activity.result = {
                        "error": {
                            "type": type(error).__name__,
                            "detail": "Extraction failed; inspect the worker log for context.",
                        }
                    }
            raise

    def _search_documents_for_asset(self, asset_id: UUID) -> list[SearchDocument]:
        with self.database.session() as session:
            asset = session.get(Asset, asset_id)
            if asset is None or asset.deleted_at is not None:
                raise FileNotFoundError(f"Asset {asset_id} does not exist.")
            chunks = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.asset_id == asset_id)
                    .order_by(DocumentChunk.chunk_index)
                ).all()
            )
            metadata = {
                "domains": list(asset.domain_slugs or []),
                "tags": list(asset.tags or []),
                "year": asset.year,
                "asset_type": asset.asset_type,
            }
            exact_terms = tuple(
                value
                for value in [asset.title, asset.title_zh, *list(asset.authors or [])]
                if value
            )
            documents = [
                SearchDocument(
                    document_id=f"asset:{asset.id}",
                    asset_id=str(asset.id),
                    kind="asset",
                    title=asset.title,
                    text="\n".join(
                        value for value in [asset.abstract, asset.summary, asset.content] if value
                    ),
                    metadata=metadata,
                    exact_terms=exact_terms,
                )
            ]
            documents.extend(
                SearchDocument(
                    document_id=f"chunk:{chunk.id}",
                    asset_id=str(asset.id),
                    kind=chunk.chunk_kind,
                    title=asset.title,
                    text=chunk.text_content,
                    metadata={
                        **metadata,
                        "representation_id": str(chunk.representation_id),
                        "chunk_index": chunk.chunk_index,
                    },
                    exact_terms=exact_terms,
                )
                for chunk in chunks
            )
            return documents

    def _upsert_documents(
        self,
        asset_id: UUID,
        documents: list[SearchDocument],
    ) -> dict[str, object]:
        transport = _MeiliWriteTransport(self.settings)
        try:
            MeilisearchIndex(transport).upsert(documents)
        finally:
            transport.close()
        digest = hashlib.sha256(
            "\n".join(
                f"{document.document_id}:{document.title}:{document.text}" for document in documents
            ).encode("utf-8")
        ).hexdigest()
        with self.database.session() as session:
            state = session.scalar(
                select(IndexState).where(
                    IndexState.index_name == "documents",
                    IndexState.entity_type == "asset",
                    IndexState.entity_id == asset_id,
                    IndexState.content_digest == digest,
                    IndexState.version == "1",
                )
            )
            if state is None:
                state = IndexState(
                    index_name="documents",
                    index_kind=IndexKind.KEYWORD.value,
                    entity_type="asset",
                    entity_id=asset_id,
                    content_digest=digest,
                    version="1",
                )
                session.add(state)
            state.status = IndexStatus.READY.value
            state.provider = "meilisearch"
            state.error = None
            state.indexed_at = datetime.now(UTC)
        return {"indexed_documents": len(documents), "content_digest": digest}

    def index_representation(
        self,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        representation_id = _uuid_parameter(parameters, "representation_id")
        with self.database.session() as session:
            representation = session.get(Representation, representation_id)
            if representation is None or representation.deleted_at is not None:
                raise FileNotFoundError(f"Representation {representation_id} does not exist.")
            asset_id = representation.asset_id
        return self._upsert_documents(
            asset_id,
            self._search_documents_for_asset(asset_id),
        )

    def index_asset(self, parameters: dict[str, object]) -> dict[str, object]:
        asset_id = _uuid_parameter(parameters, "asset_id")
        return self._upsert_documents(
            asset_id,
            self._search_documents_for_asset(asset_id),
        )

    def delete_asset_index(self, parameters: dict[str, object]) -> dict[str, object]:
        asset_id = _uuid_parameter(parameters, "asset_id")
        with self.database.session() as session:
            chunk_ids = list(
                session.scalars(
                    select(DocumentChunk.id).where(DocumentChunk.asset_id == asset_id)
                ).all()
            )
        document_ids = [f"asset:{asset_id}", *[f"chunk:{item}" for item in chunk_ids]]
        transport = _MeiliWriteTransport(self.settings)
        try:
            MeilisearchIndex(transport).delete(document_ids)
        finally:
            transport.close()
        with self.database.session() as session:
            states = session.scalars(
                select(IndexState).where(
                    IndexState.entity_type == "asset",
                    IndexState.entity_id == asset_id,
                )
            ).all()
            for state in states:
                state.status = IndexStatus.STALE.value
        return {"deleted_documents": len(document_ids)}


def dispatch_outbox_once(
    database: DatabaseManager,
    *,
    worker_id: str,
) -> bool:
    now = datetime.now(UTC)
    with database.session() as session:
        event = session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.status.in_([OutboxStatus.PENDING.value, OutboxStatus.FAILED.value]),
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.available_at, OutboxEvent.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return False
        event.status = OutboxStatus.PROCESSING.value
        event.locked_by = worker_id
        event.locked_until = now
        event.attempts += 1
        try:
            if event.aggregate_type in {"asset", "annotation", "reading_state"}:
                asset_id_value: Any = None
                if event.aggregate_type == "asset":
                    asset_id_value = event.aggregate_id
                elif isinstance(event.payload, dict):
                    after = event.payload.get("after")
                    if isinstance(after, dict):
                        asset_id_value = after.get("asset_id")
                if asset_id_value:
                    asset_id = UUID(str(asset_id_value))
                    job_type = (
                        "delete_asset_index"
                        if event.event_type == "asset.deleted"
                        else "index_asset"
                    )
                    dedupe = f"outbox:{event.id}:{job_type}"
                    existing = session.scalar(
                        select(Job).where(
                            Job.job_type == job_type,
                            Job.dedupe_key == dedupe,
                        )
                    )
                    if existing is None:
                        session.add(
                            Job(
                                job_type=job_type,
                                parameters={"asset_id": str(asset_id)},
                                status=JobStatus.PENDING.value,
                                dedupe_key=dedupe,
                            )
                        )
        except (TypeError, ValueError) as error:
            event.status = OutboxStatus.FAILED.value
            event.available_at = now + timedelta(
                seconds=min(300, 2 ** max(0, event.attempts - 1) * 5)
            )
            event.locked_by = None
            event.locked_until = None
            event.error = {
                "type": type(error).__name__,
                "detail": "Outbox event payload could not be dispatched.",
            }
            return True
        event.status = OutboxStatus.PUBLISHED.value
        event.published_at = now
        event.locked_by = None
        event.locked_until = None
        event.error = None
        return True
