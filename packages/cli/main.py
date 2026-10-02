from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.asset_gateway.registry import SourceRegistry

from .config import DEFAULT_SOURCES_CONFIG, load_sources, select_sources
from .inspection import (
    build_inventory_report,
    build_preflight_report,
    write_report,
)


def _emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"status: {report.get('status', 'unknown')}")
    if "generated_at" in report:
        print(f"generated_at: {report['generated_at']}")
    if "totals" in report:
        for key, value in report["totals"].items():
            print(f"{key}: {value}")
    if report.get("blocking_errors"):
        print("blocking_errors:")
        for error in report["blocking_errors"]:
            print(f"  - {error}")
    if report.get("errors"):
        print("errors:")
        for error in report["errors"]:
            print(f"  - {error}")


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-asset-hub")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_SOURCES_CONFIG,
        help="Path to sources.yaml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("preflight", "inventory", "validate", "status"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--json", action="store_true")
        if name in {"inventory", "validate"}:
            subparser.add_argument("--all", action="store_true")
            subparser.add_argument("--source")

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--all", action="store_true")
    ingest.add_argument("--source")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--json", action="store_true")

    retirement = subparsers.add_parser("source-retirement")
    retirement.add_argument("--database-url")
    lifecycle = retirement.add_subparsers(dest="lifecycle_command", required=True)

    preview = lifecycle.add_parser("preview")
    preview.add_argument("--source")
    preview.add_argument("--backup-id", required=True)
    preview.add_argument("--mapping-file", type=Path)
    preview.add_argument("--external-successor-snippet", type=Path)
    preview.add_argument("--requested-by", required=True)
    preview.add_argument("--reason")
    preview.add_argument("--output", type=Path)
    preview.add_argument("--json", action="store_true")

    apply = lifecycle.add_parser("apply")
    apply.add_argument("--preview-file", type=Path, action="append", required=True)
    apply.add_argument("--expect-preview", required=True)
    _add_signing_arguments(apply)
    apply.add_argument("--json", action="store_true")

    rollback = lifecycle.add_parser("rollback")
    rollback.add_argument("--retirement-id")
    rollback.add_argument("--reason")
    rollback.add_argument("--requested-by")
    rollback.add_argument("--payload-file", type=Path)
    rollback.add_argument("--expect-preview")
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--output", type=Path)
    _add_signing_arguments(rollback, required=False)
    rollback.add_argument("--json", action="store_true")

    retirement_status = lifecycle.add_parser("status")
    retirement_status.add_argument("--source")
    retirement_status.add_argument("--json", action="store_true")
    return parser


def _add_signing_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
) -> None:
    parser.add_argument("--signer-key-id", required=required)
    parser.add_argument("--signer-registry", type=Path, required=required)
    parser.add_argument("--private-key-file", type=Path)
    parser.add_argument("--private-key-env")
    parser.add_argument("--keyring-service")
    parser.add_argument("--keyring-account")


def _database_url(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("DATABASE_URL")
        or os.environ.get("HUB_DATABASE_URL")
        or (
            "postgresql+psycopg://research_hub:research_hub_local"
            "@127.0.0.1:5432/research_asset_hub"
        )
    )


def _run_preflight(args: argparse.Namespace) -> int:
    report = build_preflight_report(load_sources(args.config))
    path = write_report(report, "preflight.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _run_inventory(args: argparse.Namespace) -> int:
    sources = select_sources(load_sources(args.config), getattr(args, "source", None), args.all)
    # Keep the full registry even for a selected source: an approved replacement
    # root may belong to another source.
    report = build_inventory_report(
        sources,
        operational_registry=SourceRegistry.load(args.config),
    )
    report_name = (
        f"inventory-{args.source}.json"
        if getattr(args, "source", None)
        else "baseline-inventory.json"
    )
    path = write_report(report, report_name)
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _run_status(args: argparse.Namespace) -> int:
    report_path = Path(__file__).resolve().parents[2] / "work" / "validation"
    report = {
        "status": "bootstrap",
        "message": "Core data services are not required for dry-run ingestion.",
        "reports": {
            "preflight": str(report_path / "preflight.json"),
            "inventory": str(report_path / "baseline-inventory.json"),
            "ingest": str(report_path / "ingest-dry-run.json"),
        },
    }
    _emit(report, args.json)
    return 0


def _run_validate(args: argparse.Namespace) -> int:
    sources = select_sources(load_sources(args.config), args.source, args.all)
    preflight = build_preflight_report(sources)
    inventory = build_inventory_report(
        sources,
        operational_registry=SourceRegistry.load(args.config),
    )
    report = {
        "schema_version": 1,
        "status": (
            "passed"
            if preflight["status"] != "failed" and inventory["status"] != "failed"
            else "failed"
        ),
        "preflight": preflight,
        "inventory": inventory,
    }
    path = write_report(report, "phase-a-validation.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _run_ingest(args: argparse.Namespace) -> int:
    if args.dry_run:
        from packages.ingestion.dry_run import run_dry_run
        from packages.ingestion.formal import retired_source_contracts_for_dry_run

        sources = select_sources(load_sources(args.config), args.source, args.all)
        configured_database_url = os.environ.get("DATABASE_URL") or os.environ.get(
            "HUB_DATABASE_URL"
        )
        retirement_contracts: dict[str, dict[str, Any]] = {}
        retirement_state_checked = False
        if configured_database_url:
            engine = create_engine(configured_database_url, pool_pre_ping=True)
            try:
                try:
                    with Session(engine) as retirement_session:
                        retirement_contracts = retired_source_contracts_for_dry_run(
                            retirement_session,
                            sources,
                            require_complete=not bool(args.source),
                        )
                    retirement_state_checked = True
                except OperationalError:
                    pass
            finally:
                engine.dispose()
        report = run_dry_run(
            sources,
            retired_source_contracts=retirement_contracts,
            operational_registry=SourceRegistry.load(args.config),
        )
        report["retirement_state_checked"] = retirement_state_checked
        path = write_report(report, "ingest-dry-run.json")
        report["report_path"] = str(path)
        _emit(report, args.json)
        return 1 if report["status"] == "failed" else 0

    if args.source:
        report = {
            "schema_version": 2,
            "status": "failed",
            "error_type": "PartialFormalImportForbidden",
            "error": (
                "Formal import requires the complete source set so cross-source "
                "identity candidates cannot bypass merge review. Use --all or "
                "omit --source; --source remains available with --dry-run."
            ),
            "source_roots_modified": False,
        }
        path = write_report(report, "ingest-formal.json")
        report["report_path"] = str(path)
        _emit(report, args.json)
        return 1

    from packages.ingestion.dry_run import run_dry_run
    from packages.ingestion.formal import (
        retired_source_contracts_for_dry_run,
        run_formal_import,
    )
    sources = select_sources(load_sources(args.config), args.source, args.all)
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        try:
            with Session(engine) as retirement_session:
                retirement_contracts = retired_source_contracts_for_dry_run(
                    retirement_session,
                    sources,
                )
            retirement_state_checked = True
        except OperationalError:
            # Preserve the pre-retirement bootstrap/test path when lifecycle
            # tables do not exist yet. The real formal importer performs the
            # same check again and therefore still fails closed before writes.
            retirement_contracts = {}
            retirement_state_checked = False
        dry_run_report = run_dry_run(
            sources,
            retired_source_contracts=retirement_contracts,
            operational_registry=SourceRegistry.load(args.config),
        )
        dry_run_report["retirement_state_checked"] = retirement_state_checked
        write_report(dry_run_report, "ingest-dry-run.json")
        if dry_run_report["status"] != "passed":
            report = {
                "schema_version": 2,
                "status": "failed",
                "error_type": "FormalDryRunGateFailed",
                "error": (
                    "Formal import requires a successful retirement-aware dry-run "
                    "over the exact complete source configuration."
                ),
                "source_roots_modified": False,
                "dry_run_status": dry_run_report["status"],
            }
        else:
            with Session(engine) as session:
                report = run_formal_import(session, sources)
                report["validated_dry_run"] = {
                    "status": dry_run_report["status"],
                    "generated_at": dry_run_report["generated_at"],
                    "normalized_records": dry_run_report["totals"][
                        "normalized_records"
                    ],
                    "active_sources": dry_run_report["active_sources"],
                    "retired_sources_verified": dry_run_report[
                        "retired_sources_verified"
                    ],
                    "historical_records_preserved": dry_run_report[
                        "historical_records_preserved"
                    ],
                    "unapproved_review_groups": len(
                        dry_run_report["reconciliation"]["unapproved_review_groups"]
                    ),
                }
                session.commit()
    except Exception as exc:
        report = {
            "schema_version": 2,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "source_roots_modified": False,
        }
    finally:
        engine.dispose()
    path = write_report(report, "ingest-formal.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"JSON input could not be read: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must contain an object: {path}")
    return value


def _read_structured_object(path: Path) -> dict[str, Any]:
    """Read a JSON/YAML operator artifact without evaluating custom tags."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Structured input could not be read: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Structured input must contain an object: {path}")
    return value


def _read_external_successor_snippet(path: Path) -> dict[str, Any]:
    """Load a full JSON snippet or verify a YAML source block against its JSON peer."""

    document = _read_structured_object(path)
    if "retirement_preview" in document:
        return document
    if path.suffix.casefold() not in {".yaml", ".yml"}:
        raise ValueError("external-successor snippet lacks retirement mapping evidence")
    companion = path.with_suffix(".json")
    full_document = _read_structured_object(companion)
    if document.get("sources") != full_document.get("sources"):
        raise ValueError(
            "YAML external-successor sources do not match the JSON mapping companion"
        )
    return full_document


def _write_explicit_json(path: Path | None, value: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _mapping_values(document: dict[str, Any], name: str) -> dict[str, str]:
    value = document.get(name, {})
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an object or list")
    key_names = {
        "record_mappings": ("retired_source_record_id", "successor_source_record_id"),
        "representation_mappings": (
            "retired_representation_id",
            "successor_representation_id",
        ),
        "mount_mappings": ("retired_mount_id", "successor_mount_id"),
    }
    retired_key, successor_key = key_names[name]
    result: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict) or retired_key not in row or successor_key not in row:
            raise ValueError(f"{name} contains an incomplete mapping")
        result[str(row[retired_key])] = str(row[successor_key])
    return result


def _source_id(session: Session, value: str) -> UUID:
    from packages.domain.models import Source

    try:
        identifier = UUID(value)
    except ValueError:
        source = session.scalar(select(Source).where(Source.alias == value))
    else:
        source = session.get(Source, identifier)
    if source is None:
        raise ValueError(f"Unknown source {value!r}")
    return source.id


def _signing_material(args: argparse.Namespace) -> tuple[Any, Any]:
    from packages.ingestion.source_lifecycle_signing import (
        PublicKeyRegistry,
        load_private_key,
    )

    if not args.signer_key_id or not args.signer_registry:
        raise ValueError("signer key id and public registry are required")
    private_key = load_private_key(
        private_key_file=args.private_key_file,
        private_key_env=args.private_key_env,
        keyring_service=args.keyring_service,
        keyring_account=args.keyring_account,
    )
    registry = PublicKeyRegistry.load(args.signer_registry)
    return private_key, registry


def _external_successor_groups(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and group an external-successor snippet by retired source alias."""

    if document.get("schema_version") != 1:
        raise ValueError("external-successor snippet schema_version must be 1")
    if document.get("report_kind") != "external_successor_source_registration_snippet":
        raise ValueError("input is not an external-successor source snippet")
    retirement = document.get("retirement_preview")
    if not isinstance(retirement, dict):
        raise ValueError("external-successor snippet lacks retirement_preview")
    if retirement.get("formal_reference_coverage_complete") is not True:
        raise ValueError("external-successor formal reference coverage is incomplete")
    if retirement.get("uncovered_formal_references") not in ([], None):
        raise ValueError("external-successor snippet contains uncovered formal references")
    record_rows = document.get("record_mappings")
    representation_rows = document.get("representation_mappings")
    if not isinstance(record_rows, list) or not isinstance(representation_rows, list):
        raise ValueError("external-successor mappings must be lists")

    declared_old = retirement.get("old_source_ids")
    declared_new = retirement.get("successor_source_ids")
    if not isinstance(declared_old, list) or not isinstance(declared_new, list):
        raise ValueError("retirement_preview must declare old and successor source ids")
    old_aliases = [str(value) for value in declared_old]
    new_aliases = [str(value) for value in declared_new]
    if not old_aliases or len(old_aliases) != len(set(old_aliases)):
        raise ValueError("old_source_ids must be a non-empty unique list")
    if len(new_aliases) != len(set(new_aliases)):
        raise ValueError("successor_source_ids must be unique")

    groups: dict[str, dict[str, Any]] = {
        alias: {
            "retired_source": alias,
            "successor_source": None,
            "record_mappings": [],
            "representation_mappings": [],
        }
        for alias in old_aliases
    }

    def mapping_pair(row: Any, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(row, dict):
            raise ValueError(f"external-successor {kind} mapping is not an object")
        old = row.get("old")
        new = row.get("new")
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise ValueError(f"external-successor {kind} mapping is incomplete")
        return old, new

    for row in record_rows:
        old, new = mapping_pair(row, "record")
        old_alias = str(old.get("source_id", ""))
        new_alias = str(new.get("source_id", ""))
        group = groups.get(old_alias)
        if group is None or new_alias not in new_aliases:
            raise ValueError("record mapping references an undeclared source")
        prior = group["successor_source"]
        if prior not in (None, new_alias):
            raise ValueError(f"retired source {old_alias!r} maps to multiple successors")
        group["successor_source"] = new_alias
        group["record_mappings"].append((old, new))

    for row in representation_rows:
        old, new = mapping_pair(row, "representation")
        old_alias = str(old.get("source_id", ""))
        new_alias = str(new.get("source_id", ""))
        group = groups.get(old_alias)
        if group is None or new_alias not in new_aliases:
            raise ValueError("representation mapping references an undeclared source")
        prior = group["successor_source"]
        if prior not in (None, new_alias):
            raise ValueError(f"retired source {old_alias!r} maps to multiple successors")
        group["successor_source"] = new_alias
        group["representation_mappings"].append((old, new))

    mapped_successors = {str(group["successor_source"]) for group in groups.values()}
    if None in {group["successor_source"] for group in groups.values()}:
        raise ValueError("every retired source must have one successor")
    if mapped_successors != set(new_aliases):
        raise ValueError("declared successor sources do not match mapping evidence")
    if retirement.get("mapping_count") != len(record_rows):
        raise ValueError("record mapping count disagrees with retirement_preview")
    if retirement.get("representation_mapping_count") != len(representation_rows):
        raise ValueError("representation mapping count disagrees with retirement_preview")
    return [groups[alias] for alias in old_aliases]


def _normalize_representation_type(value: Any) -> set[str]:
    normalized = str(value or "").casefold()
    aliases = {
        "code": {"code", "repository"},
        "repository": {"code", "repository"},
        "note": {"note", "markdown"},
        "markdown": {"note", "markdown"},
    }
    return aliases.get(normalized, {normalized})


def _normalized_path(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).replace("\\", "/").strip("/")


def _normalized_url(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().rstrip("/")
    return cleaned[:-4] if cleaned.casefold().endswith(".git") else cleaned


def _resolve_snippet_record(
    session: Session,
    *,
    source_id: UUID,
    selector: dict[str, Any],
) -> Any:
    from packages.domain.models import SourceRecord

    record_id = str(selector.get("source_record_id", ""))
    if not record_id:
        raise ValueError("snippet record selector lacks source_record_id")
    matches = list(
        session.scalars(
            select(SourceRecord).where(
                SourceRecord.source_id == source_id,
                SourceRecord.source_record_id == record_id,
            )
        ).all()
    )
    if len(matches) != 1:
        raise ValueError(
            f"record selector {record_id!r} resolved to {len(matches)} database rows"
        )
    return matches[0]


def _resolve_snippet_representation(
    session: Session,
    *,
    source_id: UUID,
    source_record_id: UUID,
    selector: dict[str, Any],
) -> Any:
    from packages.domain.models import Representation

    candidates = list(
        session.scalars(
            select(Representation).where(
                Representation.source_id == source_id,
                Representation.source_record_id == source_record_id,
                Representation.deleted_at.is_(None),
            )
        ).all()
    )
    accepted_types = _normalize_representation_type(selector.get("type"))
    root_key = selector.get("asset_root_key")
    path = _normalized_path(selector.get("path"))
    url = _normalized_url(selector.get("url"))
    matches = [
        candidate
        for candidate in candidates
        if candidate.representation_type.casefold() in accepted_types
        and (root_key is None or candidate.root_key == str(root_key))
        and (path is None or _normalized_path(candidate.path_ref) == path)
        and (url is None or _normalized_url(candidate.external_url) == url)
    ]
    if len(matches) != 1:
        raise ValueError(
            "representation selector resolved to "
            f"{len(matches)} rows for source_record_id={selector.get('source_record_id')!r}, "
            f"type={selector.get('type')!r}, path={selector.get('path')!r}"
        )
    return matches[0]


def _snippet_safe_tree_integrity(selector: dict[str, Any]) -> dict[str, Any] | None:
    """Read the strict safe-copy directory declaration, never a partial hint."""

    integrity = selector.get("integrity")
    if integrity is None:
        return None
    if not isinstance(integrity, dict):
        raise ValueError("representation integrity must be an object")
    tree_fields = {"tree_sha256", "file_count", "aggregate_size_bytes"}
    if not (tree_fields & set(integrity)):
        return None
    if not tree_fields <= set(integrity):
        raise ValueError(
            "directory integrity requires tree_sha256, file_count, and aggregate_size_bytes"
        )
    digest = integrity["tree_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or digest.casefold() != digest
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("directory integrity tree_sha256 must be a lowercase SHA-256")
    counts: dict[str, int] = {}
    for name in ("file_count", "aggregate_size_bytes"):
        value = integrity[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"directory integrity {name} must be a non-negative integer")
        counts[name] = value
    return {"tree_sha256": digest, **counts}


def _previews_from_external_successor(
    session: Session,
    *,
    document: dict[str, Any],
    backup_id: str,
    requested_by: str,
    reason: str | None,
) -> list[Any]:
    from packages.domain.models import RepresentationMount
    from packages.ingestion.source_retirement import preview_source_retirement

    previews: list[Any] = []
    for group in _external_successor_groups(document):
        retired_source_id = _source_id(session, str(group["retired_source"]))
        successor_source_id = _source_id(session, str(group["successor_source"]))
        record_mappings: dict[UUID, UUID] = {}
        for old_selector, new_selector in group["record_mappings"]:
            old_record = _resolve_snippet_record(
                session,
                source_id=retired_source_id,
                selector=old_selector,
            )
            new_record = _resolve_snippet_record(
                session,
                source_id=successor_source_id,
                selector=new_selector,
            )
            if old_record.id in record_mappings:
                raise ValueError(f"duplicate retired record mapping {old_record.id}")
            record_mappings[old_record.id] = new_record.id

        representation_mappings: dict[UUID, UUID] = {}
        new_representations: dict[UUID, Any] = {}
        safe_tree_expectations: dict[UUID, dict[str, Any]] = {}
        for old_selector, new_selector in group["representation_mappings"]:
            old_record = _resolve_snippet_record(
                session,
                source_id=retired_source_id,
                selector=old_selector,
            )
            new_record = _resolve_snippet_record(
                session,
                source_id=successor_source_id,
                selector=new_selector,
            )
            old_representation = _resolve_snippet_representation(
                session,
                source_id=retired_source_id,
                source_record_id=old_record.id,
                selector=old_selector,
            )
            new_representation = _resolve_snippet_representation(
                session,
                source_id=successor_source_id,
                source_record_id=new_record.id,
                selector=new_selector,
            )
            if old_representation.id in representation_mappings:
                raise ValueError(
                    f"duplicate retired representation mapping {old_representation.id}"
                )
            representation_mappings[old_representation.id] = new_representation.id
            new_representations[new_representation.id] = new_representation
            old_safe_tree = _snippet_safe_tree_integrity(old_selector)
            new_safe_tree = _snippet_safe_tree_integrity(new_selector)
            if old_safe_tree is not None and new_safe_tree is None:
                raise ValueError(
                    "safe-tree integrity must be declared on the successor selector"
                )
            if (
                old_safe_tree is not None
                and new_safe_tree is not None
                and old_safe_tree != new_safe_tree
            ):
                raise ValueError(
                    "retired and successor safe-tree declarations disagree"
                )
            if new_safe_tree is not None:
                if (
                    old_representation.materialization_kind != "directory"
                    or new_representation.materialization_kind != "directory"
                    or not old_representation.path_ref
                    or not new_representation.path_ref
                    or not old_selector.get("path")
                    or not new_selector.get("path")
                ):
                    raise ValueError(
                        "safe-tree evidence is allowed only for explicit local "
                        "directory representations"
                    )
                safe_tree_expectations[old_representation.id] = {
                    "successor_representation_id": new_representation.id,
                    **new_safe_tree,
                }

        retired_representation_ids = set(representation_mappings)
        mount_statement = select(RepresentationMount).where(
            RepresentationMount.source_id == retired_source_id
        )
        if retired_representation_ids:
            mount_statement = select(RepresentationMount).where(
                (RepresentationMount.source_id == retired_source_id)
                | (
                    RepresentationMount.representation_id.in_(
                        retired_representation_ids
                    )
                )
            )
        retired_mounts = list(session.scalars(mount_statement).all())
        mount_mappings: dict[UUID, UUID] = {}
        for old_mount in retired_mounts:
            successor_representation_id = representation_mappings.get(
                old_mount.representation_id
            )
            if successor_representation_id is None:
                raise ValueError(
                    f"retired mount {old_mount.id} has no representation mapping"
                )
            successor_representation = new_representations[
                successor_representation_id
            ]
            successor_mounts = list(
                session.scalars(
                    select(RepresentationMount).where(
                        RepresentationMount.representation_id
                        == successor_representation_id,
                        RepresentationMount.source_id
                        == successor_representation.source_id,
                        RepresentationMount.mount_path == old_mount.mount_path,
                    )
                ).all()
            )
            if len(successor_mounts) != 1:
                raise ValueError(
                    f"retired mount {old_mount.id} resolved to "
                    f"{len(successor_mounts)} successor mounts"
                )
            mount_mappings[old_mount.id] = successor_mounts[0].id

        previews.append(
            preview_source_retirement(
                session,
                source_id=retired_source_id,
                backup_id=backup_id,
                record_mappings=record_mappings,
                representation_mappings=representation_mappings,
                mount_mappings=mount_mappings,
                requested_by=requested_by,
                reason=reason,
                safe_tree_expectations=safe_tree_expectations,
            )
        )
    return previews


def _batch_preview_report(previews: list[Any]) -> dict[str, Any]:
    from packages.ingestion.source_retirement import canonical_payload_digest

    if not previews:
        raise ValueError("retirement preview batch must not be empty")
    ordered = sorted(previews, key=lambda item: str(item.payload["source_id"]))
    source_ids = [str(item.payload["source_id"]) for item in ordered]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("retirement preview batch contains a duplicate source")
    backup_ids = {str(item.payload["backup_id"]) for item in ordered}
    if len(backup_ids) != 1:
        raise ValueError("all retirement previews in a batch must use the same backup id")
    batch_payload = {
        "schema_version": 1,
        "operation": "source-retirement-batch",
        "backup_id": next(iter(backup_ids)),
        "source_previews": [
            {
                "source_id": str(item.payload["source_id"]),
                "payload_digest": item.payload_digest,
            }
            for item in ordered
        ],
    }
    batch_digest = canonical_payload_digest(batch_payload)
    ready = all(item.ready for item in ordered)
    return {
        "schema_version": 1,
        "status": "ready" if ready else "invalid",
        "operation": "source-retirement-batch-preview",
        "read_only": True,
        "backup_id": next(iter(backup_ids)),
        "batch_payload": batch_payload,
        "batch_digest": batch_digest,
        "payload_digest": batch_digest,
        "previews": [item.as_dict() for item in ordered],
        "counts": {
            "sources": len(ordered),
            "records": sum(
                len(item.payload.get("record_mappings", [])) for item in ordered
            ),
            "representations": sum(
                len(item.payload.get("representation_mappings", []))
                for item in ordered
            ),
            "mounts": sum(
                len(item.payload.get("mount_mappings", [])) for item in ordered
            ),
        },
    }


def _run_source_retirement_preview(args: argparse.Namespace) -> int:
    from packages.ingestion.source_retirement import preview_source_retirement

    snippet_mode = args.external_successor_snippet is not None
    if snippet_mode == bool(args.source or args.mapping_file):
        raise ValueError(
            "select either --external-successor-snippet or both --source and --mapping-file"
        )
    if not snippet_mode and (not args.source or args.mapping_file is None):
        raise ValueError("single-source preview requires --source and --mapping-file")
    engine = create_engine(_database_url(args.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            if snippet_mode:
                previews = _previews_from_external_successor(
                    session,
                    document=_read_external_successor_snippet(
                        args.external_successor_snippet
                    ),
                    backup_id=args.backup_id,
                    requested_by=args.requested_by,
                    reason=args.reason,
                )
                report = _batch_preview_report(previews)
                ready = all(item.ready for item in previews)
            else:
                mappings = _read_json_object(args.mapping_file)
                preview = preview_source_retirement(
                    session,
                    source_id=_source_id(session, args.source),
                    backup_id=args.backup_id,
                    record_mappings=_mapping_values(mappings, "record_mappings"),
                    representation_mappings=_mapping_values(
                        mappings, "representation_mappings"
                    ),
                    mount_mappings=_mapping_values(mappings, "mount_mappings"),
                    requested_by=args.requested_by,
                    reason=args.reason,
                )
                report = preview.as_dict()
                report["operation"] = "source-retirement-preview"
                report["read_only"] = True
                report["backup_id"] = str(preview.payload["backup_id"])
                ready = preview.ready
            session.rollback()
    finally:
        engine.dispose()
    _write_explicit_json(args.output, report)
    _emit(report, args.json)
    return 0 if ready else 1


def _preview_from_document(document: dict[str, Any]) -> Any:
    from packages.ingestion.source_retirement import (
        SourceRetirementPreview,
        canonical_payload_digest,
    )

    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("preview file does not contain a canonical payload")
    digest = canonical_payload_digest(payload)
    recorded_digest = document.get("payload_digest")
    if recorded_digest != digest:
        raise ValueError("preview file payload digest is invalid")
    if document.get("status") != "ready" or document.get("issues"):
        raise ValueError("only a ready, issue-free preview can be applied")
    return SourceRetirementPreview(
        payload=payload,
        payload_digest=digest,
        issues=(),
        warnings=(),
    )


def _preview_from_file(path: Path, expected_digest: str) -> Any:
    preview = _preview_from_document(_read_json_object(path))
    if expected_digest != preview.payload_digest:
        raise ValueError(
            "--expect-preview does not match the canonical preview payload digest"
        )
    return preview


def _preview_collection(
    paths: list[Path],
) -> tuple[list[Any], str, str, bool]:
    if not paths:
        raise ValueError("at least one preview file is required")
    documents = [_read_json_object(path) for path in paths]
    batch_documents = [document for document in documents if "previews" in document]
    if batch_documents:
        if len(documents) != 1 or len(batch_documents) != 1:
            raise ValueError("a batch preview record cannot be mixed with other preview files")
        rows = batch_documents[0].get("previews")
        if not isinstance(rows, list) or not rows:
            raise ValueError("batch preview record contains no source previews")
        previews = [
            _preview_from_document(row)
            for row in rows
            if isinstance(row, dict)
        ]
        if len(previews) != len(rows):
            raise ValueError("batch preview contains a non-object source preview")
        regenerated = _batch_preview_report(previews)
        document = batch_documents[0]
        if (
            document.get("status") != "ready"
            or document.get("batch_payload") != regenerated["batch_payload"]
            or document.get("batch_digest") != regenerated["batch_digest"]
            or document.get("payload_digest") != regenerated["payload_digest"]
        ):
            raise ValueError("batch preview record digest or payload is invalid")
        return (
            previews,
            str(regenerated["batch_digest"]),
            str(regenerated["backup_id"]),
            True,
        )

    previews = [_preview_from_document(document) for document in documents]
    if len(previews) == 1:
        preview = previews[0]
        return (
            previews,
            preview.payload_digest,
            str(preview.payload["backup_id"]),
            False,
        )
    batch = _batch_preview_report(previews)
    return previews, str(batch["batch_digest"]), str(batch["backup_id"]), True


def _run_source_retirement_apply(args: argparse.Namespace) -> int:
    from packages.ingestion.source_lifecycle_signing import sign_payload
    from packages.ingestion.source_retirement import (
        SignedSourceRetirementPreview,
        apply_source_retirement_batch,
    )

    previews, expected_digest, backup_id, batch_mode = _preview_collection(
        args.preview_file
    )
    if args.expect_preview != expected_digest:
        raise ValueError(
            "--expect-preview does not match the canonical "
            f"{'batch' if batch_mode else 'source'} preview digest"
        )
    private_key, registry = _signing_material(args)
    signed_previews = [
        SignedSourceRetirementPreview(
            preview=preview,
            signature=sign_payload(private_key, preview.payload),
            signer_key_id=args.signer_key_id,
        )
        for preview in previews
    ]
    engine = create_engine(_database_url(args.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            batch_report = apply_source_retirement_batch(
                session,
                signed_previews=signed_previews,
                signature_verifier=registry,
            )
    finally:
        engine.dispose()
    report = (
        dict(batch_report["items"][0])
        if not batch_mode and len(previews) == 1
        else batch_report
    )
    report["backup_id"] = backup_id
    report["expected_preview"] = args.expect_preview
    report["signed_preview_count"] = len(previews)
    if batch_mode:
        report["batch_digest"] = expected_digest
    _emit(report, args.json)
    return 0


def _run_source_retirement_rollback(args: argparse.Namespace) -> int:
    from packages.ingestion.source_lifecycle_signing import (
        canonical_payload_digest,
        sign_payload,
    )
    from packages.ingestion.source_retirement import (
        preview_source_retirement_rollback,
        rollback_source_retirement,
    )

    if args.payload_file is not None:
        payload_document = _read_json_object(args.payload_file)
        raw_payload = payload_document.get("payload", payload_document)
        if not isinstance(raw_payload, dict):
            raise ValueError("rollback payload file is invalid")
        payload = raw_payload
    else:
        if not args.retirement_id or not args.reason or not args.requested_by:
            raise ValueError(
                "retirement id, reason, and requested-by are required to preview rollback"
            )
        engine = create_engine(_database_url(args.database_url), pool_pre_ping=True)
        try:
            with Session(engine) as session:
                payload = preview_source_retirement_rollback(
                    session,
                    retirement_id=args.retirement_id,
                    reason=args.reason,
                    requested_by=args.requested_by,
                )
                session.rollback()
        finally:
            engine.dispose()
    payload_digest = canonical_payload_digest(payload)
    preview_report = {
        "status": "ready",
        "operation": "source-retirement-rollback",
        "read_only": not args.apply,
        "payload": payload,
        "payload_digest": payload_digest,
        "backup_id": str(payload["backup_id"]),
    }
    if not args.apply:
        _write_explicit_json(args.output, preview_report)
        _emit(preview_report, args.json)
        return 0
    if args.expect_preview != payload_digest:
        raise ValueError(
            "--expect-preview is required and must match the rollback payload digest"
        )
    private_key, registry = _signing_material(args)
    signature = sign_payload(private_key, payload)
    engine = create_engine(_database_url(args.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            report = rollback_source_retirement(
                session,
                payload=payload,
                signature=signature,
                signer_key_id=args.signer_key_id,
                signature_verifier=registry,
            )
    finally:
        engine.dispose()
    report["backup_id"] = str(payload["backup_id"])
    report["expected_preview"] = args.expect_preview
    _emit(report, args.json)
    return 0


def _run_source_retirement_status(args: argparse.Namespace) -> int:
    from packages.domain.models import (
        Source,
        SourceRecord,
        SourceRetirement,
        SourceRetirementRollback,
    )

    engine = create_engine(_database_url(args.database_url), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            statement = select(Source).order_by(Source.alias)
            if args.source:
                try:
                    source_identifier = UUID(args.source)
                except ValueError:
                    statement = statement.where(Source.alias == args.source)
                else:
                    statement = statement.where(Source.id == source_identifier)
            sources = list(session.scalars(statement).all())
            if args.source and not sources:
                raise ValueError(f"Unknown source {args.source!r}")
            rows: list[dict[str, Any]] = []
            for source in sources:
                retirements = list(
                    session.scalars(
                        select(SourceRetirement)
                        .where(SourceRetirement.source_id == source.id)
                        .order_by(SourceRetirement.applied_at)
                    ).all()
                )
                active_retirement = None
                for retirement in retirements:
                    rolled_back = session.scalar(
                        select(SourceRetirementRollback.id).where(
                            SourceRetirementRollback.retirement_id == retirement.id
                        )
                    )
                    if rolled_back is None:
                        active_retirement = retirement
                rows.append(
                    {
                        "source_id": str(source.id),
                        "source_alias": source.alias,
                        "enabled": source.enabled,
                        "lifecycle_status": (
                            "retired" if active_retirement is not None else "active"
                        ),
                        "active_retirement_id": (
                            None
                            if active_retirement is None
                            else str(active_retirement.id)
                        ),
                        "backup_id": (
                            None
                            if active_retirement is None
                            else str(active_retirement.backup_id)
                        ),
                        "payload_digest": (
                            None
                            if active_retirement is None
                            else active_retirement.payload_digest
                        ),
                        "historical_records_preserved": int(
                            session.scalar(
                                select(func.count())
                                .select_from(SourceRecord)
                                .where(SourceRecord.source_id == source.id)
                            )
                            or 0
                        ),
                    }
                )
            session.rollback()
    finally:
        engine.dispose()
    retired_count = sum(row["lifecycle_status"] == "retired" for row in rows)
    report = {
        "status": "passed",
        "sources": rows,
        "active_sources": len(rows) - retired_count,
        "retired_sources_verified": retired_count,
        "historical_records_preserved": sum(
            row["historical_records_preserved"]
            for row in rows
            if row["lifecycle_status"] == "retired"
        ),
    }
    _emit(report, args.json)
    return 0


def _run_source_retirement(args: argparse.Namespace) -> int:
    handlers = {
        "preview": _run_source_retirement_preview,
        "apply": _run_source_retirement_apply,
        "rollback": _run_source_retirement_rollback,
        "status": _run_source_retirement_status,
    }
    try:
        return handlers[args.lifecycle_command](args)
    except Exception as exc:
        report = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "source_roots_modified": False,
        }
        _emit(report, getattr(args, "json", False))
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _base_parser()
    args = parser.parse_args(argv)
    handlers = {
        "preflight": _run_preflight,
        "inventory": _run_inventory,
        "validate": _run_validate,
        "status": _run_status,
        "ingest": _run_ingest,
        "source-retirement": _run_source_retirement,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
