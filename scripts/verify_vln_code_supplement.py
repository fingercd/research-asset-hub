"""Read-only audit of the 27 code decisions and two local VLN implementations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, text

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json
from packages.domain.models import (
    Activity,
    Asset,
    DocumentChunk,
    Job,
    Relation,
    RelationEvidenceModel,
    Representation,
    Source,
    SourceRecord,
)
from packages.ingestion.safe_tree_policy import content_tree_digest, scan_directory

SOURCE_ALIAS = "vln-release-status-code-supplement-20260912"
VALIDATION = PROJECT_ROOT / "work" / "validation"
REPORT_PATH = VALIDATION / "vln-code-supplement-integration-20260912.json"
EXPECTED_STATUSES = {
    "public_implementation": 2,
    "official_placeholder": 8,
    "announced_unreleased": 2,
    "unknown": 15,
}


def _tree_state(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    files = scan_directory(root, root, skipped, errors)
    state = {
        "tree_sha256": content_tree_digest(files),
        "file_count": len(files),
        "skipped": [
            {"relative_path": item["relative_path"], "reason": item["reason"]} for item in skipped
        ],
        "scan_errors": [
            {"relative_path": item["relative_path"], "reason": item["reason"]} for item in errors
        ],
    }
    return state, files


def _git_state(root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    base = ["git", "-c", "core.fsmonitor=false", "-C", str(root)]
    options: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 30,
        "env": environment,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    try:
        head = subprocess.run([*base, "rev-parse", "HEAD"], **options)
        status = subprocess.run(
            [*base, "status", "--porcelain=v1", "--untracked-files=all"], **options
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": type(error).__name__, "clean": False}
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "head_exit_code": head.returncode,
        "status_exit_code": status.returncode,
        "clean": status.returncode == 0 and not status.stdout.strip(),
    }


def _read_api_file(
    client: httpx.Client, representation_id: str, root: Path, relative_path: str
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": relative_path, "errors": []}
    parent = PurePosixPath(relative_path).parent.as_posix()
    tree = client.get(
        f"/api/v1/representations/{representation_id}/code/tree",
        params={"path": "" if parent == "." else parent},
    )
    tree.raise_for_status()
    payload = tree.json()
    if payload.get("representation_id") != representation_id:
        result["errors"].append("tree_representation_mismatch")
    if not any(
        item.get("path") == relative_path and item.get("kind") == "file"
        for item in payload.get("entries", [])
    ):
        result["errors"].append("file_not_listed_in_code_tree")
    response = client.get(
        f"/api/v1/representations/{representation_id}/code/file", params={"path": relative_path}
    )
    response.raise_for_status()
    file_payload = response.json()
    local = root.joinpath(*PurePosixPath(relative_path).parts)
    expected = local.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    content = file_payload.get("content")
    checks = {
        "representation": file_payload.get("representation_id") == representation_id,
        "path": file_payload.get("path") == relative_path,
        "previewable": file_payload.get("previewable") is True,
        "not_truncated": file_payload.get("truncated") is False,
        "content": isinstance(content, str) and bool(content.strip()) and content == expected,
        "size": file_payload.get("size_bytes") == local.stat().st_size,
    }
    result["errors"].extend(f"file_{name}_mismatch" for name, ok in checks.items() if not ok)
    result["content_sha256"] = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    result["size_bytes"] = local.stat().st_size
    result["code_executed"] = False
    return result


def verify(*, skip_api: bool, api_base: str) -> dict[str, Any]:
    manifest_path = PROJECT_ROOT / "config" / "intake" / SOURCE_ALIAS / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    previous = json.loads(
        (VALIDATION / "vln-code-action-existing-evidence-20260912.json").read_text(encoding="utf-8")
    )
    materialization = json.loads(
        (VALIDATION / "vln-code-research-20260912" / "materialization.json").read_text(
            encoding="utf-8"
        )
    )
    expected_by_id = {row["asset_id"]: row for row in previous["items"]}
    manifest_by_id = {
        item["attributes"]["code_audit"]["source_asset_id"]: item for item in manifest["items"]
    }
    materialized_by_id = {item["asset_id"]: item for item in materialization["materialized"]}
    errors: list[str] = []
    if (
        len(previous["items"]) != 27
        or len(expected_by_id) != 27
        or len(manifest["items"]) != 27
        or set(manifest_by_id) != set(expected_by_id)
    ):
        errors.append("manifest_previous_27_asset_set_mismatch")
    manifest_statuses = Counter(
        item["attributes"]["code_audit"]["status"] for item in manifest["items"]
    )
    if manifest_statuses != EXPECTED_STATUSES:
        errors.append("manifest_code_status_counts_mismatch")
    if manifest.get("source_id") != SOURCE_ALIAS:
        errors.append("manifest_source_mismatch")
    if (
        materialization.get("source_id") != SOURCE_ALIAS
        or materialization.get("statuses") != EXPECTED_STATUSES
        or len(materialization["materialized"]) != 2
    ):
        errors.append("materialization_source_or_counts_mismatch")

    settings = get_settings()
    registry = SourceRegistry.load(settings.sources_config_path)
    database = DatabaseManager(settings)
    try:
        with database.session() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            source = session.scalar(select(Source).where(Source.alias == SOURCE_ALIAS))
            if source is None:
                raise RuntimeError("Code supplement source is not imported.")
            if not source.enabled or not source.read_only:
                errors.append("source_not_enabled_read_only")
            records = list(
                session.scalars(select(SourceRecord).where(SourceRecord.source_id == source.id))
            )
            representations = list(
                session.scalars(select(Representation).where(Representation.source_id == source.id))
            )
            asset_ids = {UUID(value) for value in expected_by_id}
            assets = {
                str(item.id): item
                for item in session.scalars(
                    select(Asset).where(
                        Asset.id.in_(asset_ids | {r.asset_id for r in representations})
                    )
                )
            }
            relations = list(
                session.scalars(
                    select(Relation).where(
                        Relation.subject_asset_id.in_(asset_ids),
                        Relation.relation_type == "implemented_by",
                        Relation.deleted_at.is_(None),
                    )
                )
            )
            relation_evidence = list(
                session.scalars(
                    select(RelationEvidenceModel).where(
                        RelationEvidenceModel.relation_id.in_([r.id for r in relations])
                    )
                )
            )
            representation_ids = {r.id for r in representations}
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
            chunks = list(
                session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.representation_id.in_(representation_ids)
                    )
                )
            )
            jobs = list(
                session.scalars(
                    select(Job)
                    .where(
                        Job.parameters["representation_id"]
                        .as_string()
                        .in_([str(value) for value in representation_ids])
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                )
            )
    finally:
        database.dispose()

    record_by_key = {record.source_record_id: record for record in records}
    expected_record_keys = {f"code-audit-{asset_id}" for asset_id in expected_by_id}
    if len(records) != 27 or set(record_by_key) != expected_record_keys:
        errors.append("source_record_set_mismatch")
    if len(representations) != 2 or len({r.asset_id for r in representations}) != 2:
        errors.append("source_requires_exactly_two_repository_representations")
    reps_by_record: dict[UUID | None, list[Representation]] = defaultdict(list)
    for representation in representations:
        reps_by_record[representation.source_record_id].append(representation)
    latest_activities: dict[UUID, Activity] = {}
    for past_activity in activities:
        if past_activity.representation_id is not None:
            latest_activities.setdefault(past_activity.representation_id, past_activity)
    latest_jobs: dict[tuple[str, str], Job] = {}
    for job in jobs:
        latest_jobs.setdefault((str(job.parameters.get("representation_id")), job.job_type), job)
    chunk_by_rep: dict[UUID, list[DocumentChunk]] = defaultdict(list)
    for chunk in chunks:
        chunk_by_rep[chunk.representation_id].append(chunk)

    results: list[dict[str, Any]] = []
    current_statuses: Counter[str] = Counter()
    repository_results: list[dict[str, Any]] = []
    roots: dict[str, Path] = {}
    sample_paths: dict[str, tuple[str, str]] = {}
    for asset_id, old in expected_by_id.items():
        item_errors: list[str] = []
        result: dict[str, Any] = {
            "asset_id": asset_id,
            "title": old["title"],
            "errors": item_errors,
        }
        results.append(result)
        item = manifest_by_id.get(asset_id)
        asset = assets.get(asset_id)
        record = record_by_key.get(f"code-audit-{asset_id}")
        if item is None or asset is None or record is None:
            item_errors.append("paper_manifest_or_source_record_missing")
            continue
        if str(record.asset_id) != asset_id or item["id"] != record.source_record_id:
            item_errors.append("source_record_original_paper_mapping_mismatch")
        if asset.asset_type != "paper" or asset.deleted_at is not None:
            item_errors.append("original_paper_missing_or_wrong_type")
        if "vision-language-navigation" not in (asset.domain_slugs or []):
            item_errors.append("original_paper_vln_domain_missing")
        expected_audit = item["attributes"]["code_audit"]
        audit = asset.attributes.get("code_audit", {})
        current_statuses[str(audit.get("status"))] += 1
        result["code_audit_status"] = audit.get("status")
        if audit != expected_audit:
            item_errors.append("code_audit_manifest_mismatch")
        release = asset.attributes.get("release_status", {})
        expected_release = item["attributes"]["release_status"]
        if any(release.get(key) != value for key, value in expected_release.items()):
            item_errors.append("code_release_manifest_mismatch")
        if release.get("code_verified_at") != "2026-09-12":
            item_errors.append("code_verified_at_mismatch")
        prior_release = old["release_status"]
        checkpoint_keys = {
            key
            for key in set(prior_release) | set(release)
            if key.startswith("checkpoint") or key == "verified_at"
        }
        if any(release.get(key) != prior_release.get(key) for key in checkpoint_keys):
            item_errors.append("checkpoint_or_prior_verified_at_changed")
        result["code_verified_at"] = release.get("code_verified_at")
        result["preserved_checkpoint_fields"] = {key: release.get(key) for key in checkpoint_keys}
        if record.raw_data.get("attributes") != item["attributes"]:
            item_errors.append("source_record_raw_attributes_mismatch")
        paper_reps = reps_by_record.get(record.id, [])
        public = expected_audit["status"] == "public_implementation"
        if not public:
            if paper_reps or item.get("representations", {}).get("code"):
                item_errors.append("non_public_paper_has_supplement_code_representation")
            if any(
                str(relation.subject_asset_id) == asset_id
                and relation.confirmation_status == "confirmed"
                for relation in relations
            ):
                item_errors.append("non_public_paper_has_confirmed_implementation")
            continue
        if len(paper_reps) != 1:
            item_errors.append("public_paper_requires_one_repository_representation")
            continue
        representation = paper_reps[0]
        result["code_representation_id"] = str(representation.id)
        spec = item["representations"]["code"]
        repo_asset = assets.get(str(representation.asset_id))
        repo_errors: list[str] = []
        repo_result: dict[str, Any] = {
            "paper_asset_id": asset_id,
            "repository_asset_id": str(representation.asset_id),
            "representation_id": str(representation.id),
            "repository_url": spec["url"],
            "path_ref": spec["path"],
            "revision": representation.revision,
            "errors": repo_errors,
        }
        repository_results.append(repo_result)
        checks = {
            "repository_asset": repo_asset is not None
            and repo_asset.asset_type == "repository"
            and repo_asset.deleted_at is None
            and str(repo_asset.id) != asset_id,
            "representation_type": representation.representation_type == "repository",
            "role": representation.role == "official",
            "availability": representation.availability == "available",
            "not_deleted": representation.deleted_at is None,
            "path": representation.path_ref == spec["path"],
            "root": representation.root_key == "repository",
            "url": representation.external_url == spec["url"],
            "revision": representation.revision == spec["commit"],
            "status": representation.attributes.get("status") == spec["status"],
            "license": representation.attributes.get("license") == spec["license"],
        }
        repo_errors.extend(f"{key}_mismatch" for key, ok in checks.items() if not ok)
        matching_relations = [
            r
            for r in relations
            if str(r.subject_asset_id) == asset_id
            and r.object_asset_id == representation.asset_id
            and r.confirmation_status == "confirmed"
        ]
        if len(matching_relations) != 1:
            repo_errors.append("confirmed_implementation_relation_missing_or_duplicate")
        else:
            relation = matching_relations[0]
            evidence = [
                e
                for e in relation_evidence
                if e.relation_id == relation.id
                and e.provider == "repository-link-reconciliation"
                and e.source_key == f"{SOURCE_ALIAS}:{record.source_record_id}:repository"
                and e.evidence.get("source_alias") == SOURCE_ALIAS
                and e.evidence.get("paper_asset_id") == asset_id
                and e.evidence.get("repository_asset_id") == str(representation.asset_id)
                and e.evidence.get("source_record_database_id") == str(record.id)
                and e.evidence.get("link_evidence", {}).get("repository_url") == spec["url"]
            ]
            repo_result["relation_id"] = str(relation.id)
            repo_result["source_relation_evidence_ids"] = [str(e.id) for e in evidence]
            if len(evidence) != 1:
                repo_errors.append("source_relation_evidence_missing_or_duplicate")
        root = registry.resolve(SOURCE_ALIAS, "repository", spec["path"])
        roots[asset_id] = root
        tree, safe_files = _tree_state(root)
        recorded = materialized_by_id.get(asset_id)
        repo_result["safe_tree"] = tree
        if recorded is None or (
            tree["tree_sha256"] != recorded["safe_tree"]["tree_sha256"]
            or tree["file_count"] != recorded["safe_tree"]["file_count"]
            or spec["path"] != recorded["path"]
            or spec["commit"] != recorded["commit"]
        ):
            repo_errors.append("materialization_safe_tree_or_commit_mismatch")
        if tree["scan_errors"]:
            repo_errors.append("safe_tree_scan_errors")
        git_state = _git_state(root)
        repo_result["git"] = git_state
        if git_state.get("head") != spec["commit"] or not git_state["clean"]:
            repo_errors.append("git_revision_or_worktree_changed")
        readmes = [
            f["relative_path"]
            for f in safe_files
            if PurePosixPath(f["relative_path"]).name.casefold() == "readme.md"
        ]
        python_files = [
            f["relative_path"]
            for f in safe_files
            if f["relative_path"].endswith(".py")
            and f["size_bytes"] > 0
            and PurePosixPath(f["relative_path"]).name != "__init__.py"
        ]
        if not readmes or not python_files:
            repo_errors.append("readme_or_python_source_missing")
        else:
            sample_paths[asset_id] = (
                min(readmes, key=lambda value: (len(PurePosixPath(value).parts), value)),
                min(python_files, key=lambda value: (len(PurePosixPath(value).parts), value)),
            )
            repo_result["api_sample_paths"] = sample_paths[asset_id]
        activity = latest_activities.get(representation.id)
        repo_chunks = chunk_by_rep[representation.id]
        repo_result["chunk_count"] = len(repo_chunks)
        if not repo_chunks or any(not chunk.text_content.strip() for chunk in repo_chunks):
            repo_errors.append("nonempty_extracted_chunks_required")
        if activity is None:
            repo_errors.append("extraction_activity_missing")
        else:
            repo_result["extraction_activity_id"] = str(activity.id)
            repo_result["extraction_result_status"] = activity.result.get("status")
            if activity.status != "succeeded" or activity.result.get("status") != "succeeded":
                repo_errors.append("latest_extraction_not_succeeded")
            if activity.result.get("warnings") != []:
                repo_errors.append("latest_extraction_has_warnings")
            if activity.result.get("chunk_count") != len(repo_chunks):
                repo_errors.append("extraction_chunk_count_mismatch")
            if any(chunk.activity_id != activity.id for chunk in repo_chunks):
                repo_errors.append("chunks_not_from_latest_extraction")
        for job_type in ("extract_representation", "index_representation"):
            latest_job = latest_jobs.get((str(representation.id), job_type))
            repo_result[f"{job_type}_job_status"] = latest_job.status if latest_job else None
            if latest_job is None or latest_job.status != "succeeded":
                repo_errors.append(f"{job_type}_job_not_succeeded")
            if job_type == "extract_representation" and activity is not None:
                if latest_job is None or activity.job_id != latest_job.id:
                    repo_errors.append("latest_activity_job_mismatch")

    if current_statuses != EXPECTED_STATUSES:
        errors.append("database_code_audit_status_counts_mismatch")
    public_ids = {
        asset_id
        for asset_id, row in manifest_by_id.items()
        if row["attributes"]["code_audit"]["status"] == "public_implementation"
    }
    if set(materialized_by_id) != public_ids:
        errors.append("materialization_public_paper_set_mismatch")
    api_result: dict[str, Any] = {"status": "skipped_requested", "papers_checked": 0}
    if not skip_api:
        api_result["status"] = "passed"
        with httpx.Client(base_url=api_base, timeout=60) as client:
            for row in results:
                asset_id = row["asset_id"]
                try:
                    response = client.get(f"/api/v1/assets/{asset_id}")
                    response.raise_for_status()
                    payload = response.json()
                    actions = {
                        action["kind"]: action for action in payload.get("quick_actions", [])
                    }
                    code = actions.get("code", {})
                    row["api_code_action_status"] = code.get("status")
                    expected_rep = row.get("code_representation_id")
                    if asset_id in public_ids:
                        if (
                            code.get("status") != "available"
                            or code.get("representation_id") != expected_rep
                        ):
                            row["errors"].append("api_public_code_action_mismatch")
                    elif code.get("status") != "not_provided" or code.get("representation_id"):
                        row["errors"].append("api_nonpublic_code_incorrectly_available")
                    if payload.get("attributes", {}).get("code_audit") != assets[
                        asset_id
                    ].attributes.get("code_audit"):
                        row["errors"].append("api_code_audit_metadata_mismatch")
                    api_result["papers_checked"] += 1
                except (httpx.HTTPError, ValueError) as error:
                    row["errors"].append(f"api_paper_read_failed:{type(error).__name__}")
            for repo in repository_results:
                asset_id = repo["paper_asset_id"]
                if asset_id not in sample_paths:
                    continue
                try:
                    repo["api_file_reads"] = [
                        _read_api_file(client, repo["representation_id"], roots[asset_id], path)
                        for path in sample_paths[asset_id]
                    ]
                    if any(item["errors"] for item in repo["api_file_reads"]):
                        repo["errors"].append("api_code_file_verification_failed")
                except (httpx.HTTPError, ValueError, OSError) as error:
                    repo["errors"].append(f"api_code_read_failed:{type(error).__name__}")
                after_tree, _ = _tree_state(roots[asset_id])
                if (
                    after_tree["tree_sha256"] != repo["safe_tree"]["tree_sha256"]
                    or _git_state(roots[asset_id]) != repo["git"]
                ):
                    repo["errors"].append("source_changed_during_api_reads")
        if any(row["errors"] for row in results + repository_results):
            api_result["status"] = "failed"
    failed_papers = sum(bool(row["errors"]) for row in results)
    failed_repositories = sum(bool(row["errors"]) for row in repository_results)
    failed = bool(errors or failed_papers or failed_repositories)
    return {
        "schema": "research-asset-hub.vln-code-supplement-integration/v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "failed" if failed else "passed_without_api" if skip_api else "passed",
        "source_alias": SOURCE_ALIAS,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "read_only_database": True,
        "code_executed": False,
        "source_records": len(records),
        "repository_representations": len(representations),
        "code_audit_status_counts": dict(current_statuses),
        "checked_papers": len(results),
        "failed_papers": failed_papers,
        "checked_repositories": len(repository_results),
        "failed_repositories": failed_repositories,
        "api": api_result,
        "errors": errors,
        "papers": results,
        "repositories": repository_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--api", default="http://127.0.0.1:8767")
    args = parser.parse_args()
    report = verify(skip_api=args.skip_api, api_base=args.api)
    atomic_write_json(REPORT_PATH, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"papers", "repositories"}},
            ensure_ascii=False,
        )
    )
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
