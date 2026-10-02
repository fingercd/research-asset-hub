"""Read-only integration audit of the 109 non-UAV unified VLN guides."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, text

from apps.api.config import PROJECT_ROOT, ApiSettings, get_settings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.exporters.common import atomic_write_json
from packages.domain.models import (
    Activity,
    Asset,
    DocumentChunk,
    Job,
    Representation,
    Source,
    SourceRecord,
)

SOURCE_ALIAS = "vln-unified-guides-non-uav-20260912-v1"
EXPECTED_COUNT = 109
WORK_ROOT = PROJECT_ROOT / "work" / "mineru" / "vln-20260904"
REPORT_PATH = PROJECT_ROOT / "work" / "validation" / "vln-guide-integration-20260912.json"


def _digest_file(registry: SourceRegistry, alias: str, root_key: str, path_ref: str) -> str:
    path = registry.resolve(alias, root_key, path_ref)
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _index_errors(
    client: httpx.Client, chunk: DocumentChunk, representation_digest: str | None
) -> tuple[str, list[str]]:
    logical_id = f"chunk:{chunk.id}"
    key = hashlib.sha256(logical_id.encode("utf-8")).hexdigest()
    try:
        response = client.get(f"/indexes/documents/documents/{key}")
        if response.status_code != 200:
            return str(chunk.representation_id), [f"{logical_id}:http_{response.status_code}"]
        document = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return str(chunk.representation_id), [f"{logical_id}:{type(error).__name__}"]
    if not isinstance(document, dict) or not isinstance(document.get("metadata"), dict):
        return str(chunk.representation_id), [f"{logical_id}:invalid_document_structure"]
    metadata = document["metadata"]
    comparisons = {
        "document_id": document.get("document_id") == key,
        "logical_document_id": document.get("logical_document_id") == logical_id,
        "asset_id": document.get("asset_id") == str(chunk.asset_id),
        "kind": document.get("kind") == "document",
        "text": document.get("text") == chunk.text_content,
        "representation_id": metadata.get("representation_id") == str(chunk.representation_id),
        "representation_digest": metadata.get("representation_digest") == representation_digest,
        "content_hash": metadata.get("content_hash") == chunk.content_hash,
    }
    return str(chunk.representation_id), [
        f"{logical_id}:{field}_mismatch" for field, matches in comparisons.items() if not matches
    ]


def verify(*, skip_index: bool) -> dict[str, Any]:
    inventory = json.loads((WORK_ROOT / "inventory.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (PROJECT_ROOT / "config" / "intake" / SOURCE_ALIAS / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    inventory_rows = inventory["items"]
    expected_ids = {row["asset_id"] for row in inventory_rows}
    expected_records = {f"guide-{asset_id}" for asset_id in expected_ids}
    manifest_by_record = {item["id"]: item for item in manifest["items"]}
    errors: list[str] = []
    if len(inventory_rows) != EXPECTED_COUNT or len(expected_ids) != EXPECTED_COUNT:
        errors.append("inventory_not_109_unique_assets")
    expected_digest = hashlib.sha256("\n".join(sorted(expected_ids)).encode("utf-8")).hexdigest()
    if inventory.get("asset_id_set_sha256") != expected_digest:
        errors.append("inventory_asset_set_digest_mismatch")
    if len(manifest["items"]) != EXPECTED_COUNT or set(manifest_by_record) != expected_records:
        errors.append("manifest_record_set_mismatch")
    if manifest.get("asset_id_set_sha256") != expected_digest:
        errors.append("manifest_asset_set_digest_mismatch")
    if manifest.get("source_id") != SOURCE_ALIAS:
        errors.append("manifest_source_alias_mismatch")

    settings = get_settings()
    database = DatabaseManager(settings)
    registry = SourceRegistry.load(settings.sources_config_path)
    try:
        with database.session() as session:
            # This audit cannot write, even if future code accidentally changes an ORM row.
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            source = session.scalar(select(Source).where(Source.alias == SOURCE_ALIAS))
            if source is None:
                errors.append("source_missing")
                source_records: list[SourceRecord] = []
                representations: list[Representation] = []
            else:
                source_records = list(
                    session.scalars(select(SourceRecord).where(SourceRecord.source_id == source.id))
                )
                representations = list(
                    session.scalars(
                        select(Representation).where(Representation.source_id == source.id)
                    )
                )
                if not source.enabled or not source.read_only:
                    errors.append("source_not_enabled_read_only")
            current_assets = {
                str(asset_id)
                for asset_id, domains in session.execute(
                    select(Asset.id, Asset.domain_slugs).where(
                        Asset.deleted_at.is_(None), Asset.asset_type == "paper"
                    )
                )
                if "vision-language-navigation" in (domains or [])
            }
            if current_assets != expected_ids:
                errors.append("current_domain_asset_set_mismatch")
            representation_ids = [item.id for item in representations]
            chunks = list(
                session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.representation_id.in_(representation_ids)
                    )
                )
            )
            activities = list(
                session.scalars(
                    select(Activity)
                    .where(
                        Activity.representation_id.in_(representation_ids),
                        Activity.activity_type == "extraction",
                    )
                    .order_by(Activity.started_at.desc(), Activity.id.desc())
                )
            )
            jobs = {
                item.id: item
                for item in session.scalars(
                    select(Job).where(Job.id.in_([item.job_id for item in activities]))
                )
            }
            pdfs = {
                str(item.id): item
                for item in session.scalars(
                    select(Representation).where(
                        Representation.id.in_(
                            [UUID(row["primary_pdf"]["id"]) for row in inventory_rows]
                        )
                    )
                )
            }
            source_aliases = {
                source_id: alias
                for source_id, alias in session.execute(select(Source.id, Source.alias))
            }
    finally:
        database.dispose()

    records_by_key = {record.source_record_id: record for record in source_records}
    if len(source_records) != EXPECTED_COUNT or set(records_by_key) != expected_records:
        errors.append("database_source_record_set_mismatch")
    if len(representations) != EXPECTED_COUNT:
        errors.append("database_representation_count_mismatch")
    if {str(item.asset_id) for item in representations} != expected_ids:
        errors.append("database_representation_asset_set_mismatch")
    reps_by_asset: dict[str, list[Representation]] = defaultdict(list)
    chunks_by_rep: dict[UUID, list[DocumentChunk]] = defaultdict(list)
    activities_by_rep: dict[UUID, Activity] = {}
    for representation in representations:
        reps_by_asset[str(representation.asset_id)].append(representation)
    for chunk in chunks:
        chunks_by_rep[chunk.representation_id].append(chunk)
    for past_activity in activities:
        if past_activity.representation_id is not None:
            activities_by_rep.setdefault(past_activity.representation_id, past_activity)

    results: list[dict[str, Any]] = []
    for inventory_row in inventory_rows:
        asset_id = inventory_row["asset_id"]
        item_errors: list[str] = []
        result: dict[str, Any] = {"asset_id": asset_id, "errors": item_errors}
        record_key = f"guide-{asset_id}"
        record = records_by_key.get(record_key)
        if record is None or str(record.asset_id) != asset_id:
            item_errors.append("source_record_asset_mismatch")
        candidates = reps_by_asset[asset_id]
        if len(candidates) != 1:
            item_errors.append("asset_requires_exactly_one_new_representation")
        else:
            representation = candidates[0]
            result["representation_id"] = str(representation.id)
            result["html_sha256"] = representation.sha256
            guide_spec = manifest_by_record.get(record_key, {}).get("representations", {}).get(
                "guide", {}
            )
            if record is None or representation.source_record_id != record.id:
                item_errors.append("representation_source_record_link_mismatch")
            checks = {
                "type": representation.representation_type == "html",
                "role": representation.role == "guide",
                "availability": representation.availability == "available",
                "materialization": representation.materialization_kind == "file",
                "not_deleted": representation.deleted_at is None,
                "offline": not representation.network_required,
                "root_key": representation.root_key == "html",
                "manifest_path": representation.path_ref == guide_spec.get("path"),
                "manifest_sha256": representation.sha256 == guide_spec.get("sha256"),
                "unified_metadata": representation.attributes.get("unified_guide") is True,
                "static_metadata": representation.attributes.get("sandbox_policy") == "static",
                "source_metadata": representation.attributes.get("source_alias") == SOURCE_ALIAS,
            }
            item_errors.extend(f"html_{name}_mismatch" for name, ok in checks.items() if not ok)
            if representation.path_ref is None:
                item_errors.append("html_path_missing")
            else:
                try:
                    actual = _digest_file(
                        registry, SOURCE_ALIAS, representation.root_key, representation.path_ref
                    )
                    result["html_file_sha256"] = actual
                    if actual != guide_spec.get("sha256") or actual != representation.sha256:
                        item_errors.append("html_file_sha256_mismatch")
                except (OSError, UnsafeAssetPath) as error:
                    item_errors.append(f"html_file_unreadable:{type(error).__name__}")
            representation_chunks = chunks_by_rep[representation.id]
            result["chunk_count"] = len(representation_chunks)
            if not representation_chunks:
                item_errors.append("extracted_chunks_missing")
            if any(not chunk.text_content.strip() for chunk in representation_chunks):
                item_errors.append("empty_extracted_chunk")
            if any(str(chunk.asset_id) != asset_id for chunk in representation_chunks):
                item_errors.append("chunk_asset_mismatch")
            activity = activities_by_rep.get(representation.id)
            if activity is None:
                item_errors.append("extraction_activity_missing")
            else:
                result["extraction_activity_id"] = str(activity.id)
                result["extraction_status"] = activity.result.get("status")
                if activity.status != "succeeded" or activity.result.get("status") != "succeeded":
                    item_errors.append("extraction_not_succeeded")
                if activity.result.get("warnings") != []:
                    item_errors.append("extraction_warnings_or_missing_warning_record")
                if activity.result.get("chunk_count") != len(representation_chunks):
                    item_errors.append("extraction_chunk_count_mismatch")
                if any(chunk.activity_id != activity.id for chunk in representation_chunks):
                    item_errors.append("chunks_not_from_latest_extraction")
                job = jobs.get(activity.job_id) if activity.job_id is not None else None
                result["extraction_job_id"] = str(activity.job_id) if activity.job_id else None
                result["extraction_job_status"] = job.status if job else None
                if (
                    job is None
                    or job.status != "succeeded"
                    or job.job_type not in {"extract_representation", "representation.extract"}
                    or job.parameters.get("representation_id") != str(representation.id)
                ):
                    item_errors.append("extraction_job_not_succeeded_or_mismatched")

        pdf_spec = inventory_row["primary_pdf"]
        pdf = pdfs.get(pdf_spec["id"])
        result["original_pdf_sha256"] = pdf_spec["sha256"]
        if pdf is None:
            item_errors.append("original_pdf_representation_missing")
        else:
            if (
                str(pdf.asset_id) != asset_id
                or pdf.representation_type != "pdf"
                or pdf.deleted_at is not None
                or pdf.sha256 != pdf_spec["sha256"]
                or pdf.path_ref != pdf_spec["path_ref"]
                or pdf.root_key != pdf_spec["root_key"]
                or source_aliases.get(pdf.source_id) != pdf_spec["source_alias"]
            ):
                item_errors.append("original_pdf_database_inventory_mismatch")
        try:
            actual_pdf = _digest_file(
                registry, pdf_spec["source_alias"], pdf_spec["root_key"], pdf_spec["path_ref"]
            )
            result["original_pdf_file_sha256"] = actual_pdf
            if actual_pdf != pdf_spec["sha256"]:
                item_errors.append("original_pdf_file_sha256_mismatch")
        except (OSError, UnsafeAssetPath) as error:
            item_errors.append(f"original_pdf_unreadable:{type(error).__name__}")
        results.append(result)

    index_result: dict[str, Any] = {"status": "skipped_requested", "chunks_checked": 0}
    if not skip_index:
        index_result["status"] = "passed"
        # Use the source-defined local development default, never credential files.
        api_key = str(ApiSettings.model_fields["meilisearch_api_key"].default)
        by_representation = {str(item.id): item for item in representations}
        results_by_rep = {
            item["representation_id"]: item for item in results if "representation_id" in item
        }
        with httpx.Client(
            base_url=settings.meilisearch_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        ) as client:
            try:
                health = client.get("/indexes/documents/stats")
                health.raise_for_status()
            except httpx.HTTPError as error:
                errors.append(f"documents_index_unavailable:{type(error).__name__}")
                index_result["status"] = "failed"
            else:
                def check(chunk: DocumentChunk) -> tuple[str, list[str]]:
                    representation = by_representation[str(chunk.representation_id)]
                    return _index_errors(client, chunk, representation.sha256)

                with ThreadPoolExecutor(max_workers=8) as executor:
                    for representation_id, chunk_errors in executor.map(check, chunks):
                        index_result["chunks_checked"] += 1
                        if chunk_errors:
                            index_result["status"] = "failed"
                            row = results_by_rep.get(representation_id)
                            if row is None:
                                errors.extend(chunk_errors)
                            else:
                                row["errors"].extend(chunk_errors)
    failed = sum(bool(item["errors"]) for item in results)
    status = (
        "failed"
        if errors or failed
        else "passed_without_index" if skip_index else "passed"
    )
    return {
        "schema": "research-asset-hub.vln-guide-integration-audit/v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "status": status,
        "source_alias": SOURCE_ALIAS,
        "read_only_database": True,
        "expected_papers": EXPECTED_COUNT,
        "asset_id_set_sha256": expected_digest,
        "database_source_records": len(source_records),
        "database_representations": len(representations),
        "checked_papers": len(results),
        "passed_papers": len(results) - failed,
        "failed_papers": failed,
        "chunk_count": len(chunks),
        "index": index_result,
        "errors": errors,
        "items": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-index", action="store_true", help="Audit DB and files only.")
    arguments = parser.parse_args()
    report = verify(skip_index=arguments.skip_index)
    atomic_write_json(REPORT_PATH, report)
    print(json.dumps({key: value for key, value in report.items() if key != "items"}))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
