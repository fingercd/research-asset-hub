from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.enums import (
    ActivityStatus,
    AnchorStatus,
    JobStatus,
    OutboxStatus,
)
from packages.domain.models import (
    Activity,
    Annotation,
    DocumentChunk,
    Job,
    OutboxEvent,
    Representation,
    Source,
)
from packages.domain.serving_scope import (
    representation_serving_status,
    retired_representation_ids,
)
from packages.indexing import IndexBuilder, RemoteIndexSuite
from packages.indexing.builder import DEFAULT_REFRESH_INDEX_NAMES, KEYWORD_INDEX_NAMES

if TYPE_CHECKING:
    from packages.extraction import ExtractionPipeline

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
        self._extraction_pipeline = extraction_pipeline

    @property
    def extraction_pipeline(self) -> ExtractionPipeline:
        if self._extraction_pipeline is None:
            from packages.extraction import ExtractionPipeline

            self._extraction_pipeline = ExtractionPipeline(
                enable_tree_sitter=self.settings.worker_tree_sitter_enabled
            )
        return self._extraction_pipeline

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
        result = handler(runtime_parameters)
        if result.get("status") == "skipped_retired" and job_id is not None:
            with self.database.session() as session:
                job = session.get(Job, job_id, with_for_update=True)
                if job is not None:
                    job.cancel_requested = True
                    job.error = {
                        "type": "RepresentationRetired",
                        "detail": "Job skipped because its source is retired.",
                    }
        return result

    def _retirement_result(
        self,
        representation_id: UUID,
    ) -> tuple[Representation | None, dict[str, object] | None]:
        with self.database.session() as session:
            representation = session.get(Representation, representation_id)
            if representation is None or representation.deleted_at is not None:
                return representation, None
            status = representation_serving_status(session, representation)
            if status.active:
                session.expunge(representation)
                return representation, None
            return representation, {
                "status": "skipped_retired",
                "representation_id": str(representation.id),
                "successor_representation_id": (
                    str(status.successor_representation_id)
                    if status.successor_representation_id is not None
                    else None
                ),
            }

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
        _, retired_result = self._retirement_result(representation_id)
        if retired_result is not None:
            return retired_result
        representation, base_path, paths = self._representation_paths(representation_id)
        from packages.extraction import ExtractionStatus

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
                if representation_serving_status(session, representation).active:
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

    def _refresh_asset_keywords(self, asset_id: UUID) -> dict[str, object]:
        index_names = (
            KEYWORD_INDEX_NAMES
            if self.settings.search_code_index_enabled
            else DEFAULT_REFRESH_INDEX_NAMES
        )
        suite = RemoteIndexSuite.connect(
            meilisearch_url=self.settings.meilisearch_url,
            meilisearch_api_key=self.settings.meilisearch_api_key,
            index_names=index_names,
        )
        try:
            with self.database.engine.connect() as connection:
                if connection.dialect.name == "postgresql":
                    connection = connection.execution_options(isolation_level="REPEATABLE READ")
                with Session(connection) as session:
                    report = IndexBuilder(suite).refresh_keywords(
                        session,
                        asset_ids=(asset_id,),
                        index_names=index_names,
                    )
                    session.commit()
            return report.to_dict()
        finally:
            suite.close()

    def index_representation(
        self,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        representation_id = _uuid_parameter(parameters, "representation_id")
        with self.database.session() as session:
            representation = session.get(Representation, representation_id)
            if representation is None:
                raise FileNotFoundError(f"Representation {representation_id} does not exist.")
            asset_id = representation.asset_id
            serving_status = representation_serving_status(session, representation)
            deleted = representation.deleted_at is not None
        result = self._refresh_asset_keywords(asset_id)
        if deleted or not serving_status.active:
            result.update(
                {
                    "status": "skipped_deleted" if deleted else "skipped_retired",
                    "representation_id": str(representation_id),
                    "successor_representation_id": (
                        str(serving_status.successor_representation_id)
                        if serving_status.successor_representation_id is not None
                        else None
                    ),
                }
            )
        return result

    def index_asset(self, parameters: dict[str, object]) -> dict[str, object]:
        return self._refresh_asset_keywords(_uuid_parameter(parameters, "asset_id"))

    def delete_asset_index(self, parameters: dict[str, object]) -> dict[str, object]:
        return self._refresh_asset_keywords(_uuid_parameter(parameters, "asset_id"))


def dispatch_outbox_once(
    database: DatabaseManager,
    *,
    worker_id: str,
) -> bool:
    now = datetime.now(UTC)
    with database.session() as session:
        retired_jobs_cancelled = cancel_retired_pending_jobs(session, now=now)
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
            return retired_jobs_cancelled > 0
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


def cancel_retired_pending_jobs(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    """Cancel queued representation work and enqueue active-index cleanup.

    The worker calls this before claiming work. Historical extraction/index jobs
    remain auditable, while a separate asset-level rebuild removes any stale
    retired chunks from the serving index.
    """

    current = now or datetime.now(UTC)
    candidates = list(
        session.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.PENDING.value,
                Job.cancel_requested.is_(False),
                Job.job_type.in_(
                    {
                        "extract_representation",
                        "representation.extract",
                        "index_representation",
                        "representation.index",
                    }
                ),
            )
            .with_for_update(skip_locked=True)
        ).all()
    )
    representation_by_job: dict[UUID, UUID] = {}
    for job in candidates:
        raw_id = dict(job.parameters or {}).get("representation_id")
        try:
            representation_by_job[job.id] = UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
    retired_ids = retired_representation_ids(
        session,
        set(representation_by_job.values()),
    )
    if not retired_ids:
        return 0
    asset_by_representation: dict[UUID, UUID] = {
        representation_id: asset_id
        for representation_id, asset_id in session.execute(
            select(Representation.id, Representation.asset_id).where(
                Representation.id.in_(retired_ids)
            )
        )
    }
    cancelled = 0
    cleanup_asset_ids: set[UUID] = set()
    for job in candidates:
        representation_id = representation_by_job.get(job.id)
        if representation_id not in retired_ids:
            continue
        job.cancel_requested = True
        job.status = JobStatus.CANCELLED.value
        job.finished_at = current
        job.error = {
            "type": "RepresentationRetired",
            "detail": "Pending job cancelled because its source is retired.",
        }
        cancelled += 1
        asset_id = asset_by_representation.get(representation_id)
        if asset_id is not None:
            cleanup_asset_ids.add(asset_id)
    for asset_id in sorted(cleanup_asset_ids, key=str):
        dedupe_key = f"retirement-cleanup:asset:{asset_id}"
        existing = session.scalar(
            select(Job.id).where(
                Job.job_type == "index_asset",
                Job.dedupe_key == dedupe_key,
            )
        )
        if existing is None:
            session.add(
                Job(
                    job_type="index_asset",
                    parameters={"asset_id": str(asset_id)},
                    status=JobStatus.PENDING.value,
                    dedupe_key=dedupe_key,
                )
            )
    if cancelled:
        session.flush()
    return cancelled
