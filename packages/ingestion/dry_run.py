from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.asset_gateway.registry import SourceRegistry
from packages.adapters import DEFAULT_REGISTRY
from packages.cli.config import PROJECT_ROOT, LoadedSource, unregistered_source_domains
from packages.domain.path_exclusions import OperationalPathExclusions
from packages.domain.schemas import AdapterWarning, NormalizedRecord, SourceConfig

from .reconciliation import apply_weak_merge_approvals, reconcile_records


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _warning_code(warning: Any) -> str:
    return str(getattr(warning, "code", "unknown"))


def _source_config(source: LoadedSource) -> SourceConfig:
    raw = dict(source.raw)
    raw.setdefault("id", source.source_id)
    raw.setdefault("label", source.label)
    raw.setdefault("adapter", source.adapter)
    raw.setdefault("root", str(source.root))
    raw.setdefault("manifest", str(source.manifest))
    raw.setdefault("read_only", True)
    raw.setdefault("domain", source.domain)
    return SourceConfig.model_validate(raw)


def _warning_root_key(warning: AdapterWarning) -> str | None:
    root_key = warning.details.get("root_key")
    return root_key if isinstance(root_key, str) else None


def _ignored_warning_rule(
    record: NormalizedRecord,
    warning: AdapterWarning,
    *,
    policy: OperationalPathExclusions | None,
    roots: dict[tuple[str, str], Path],
) -> Any | None:
    """Return an active operational rule for one exact missing representation."""

    if policy is None or warning.code != "asset_path_missing" or warning.path is None:
        return None
    root_key = _warning_root_key(warning)
    if root_key is None or (record.source_id, root_key) not in roots:
        return None
    for representation in record.representations:
        if representation.root_key != root_key or representation.path_ref != warning.path:
            continue
        return policy.active_reference(
            record.source_id,
            root_key,
            warning.path,
            roots,
            source_record_id=record.source_record_id,
            representation_type=str(representation.representation_type),
            role=str(representation.role),
        )
    return None


def _warning_projection(
    record: NormalizedRecord,
    *,
    policy: OperationalPathExclusions | None,
    roots: dict[tuple[str, str], Path],
    applied_rules: set[str],
) -> dict[str, Any]:
    """Project warnings for diagnostics without changing the normalized record."""

    actionable: list[AdapterWarning] = []
    ignored: list[dict[str, str]] = []
    for warning in record.warnings:
        rule = _ignored_warning_rule(record, warning, policy=policy, roots=roots)
        if rule is None:
            actionable.append(warning)
            continue
        applied_rules.add(rule.id)
        ignored.append(
            {
                "code": _warning_code(warning),
                "path": warning.path or "",
                "root_key": _warning_root_key(warning) or "",
                "rule_id": rule.id,
                "reason": rule.reason,
            }
        )
    return {"actionable": actionable, "ignored": ignored}


def _record_summary(
    record: NormalizedRecord,
    *,
    warning_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = warning_projection or {
        "actionable": list(record.warnings),
        "ignored": [],
    }
    return {
        "source_id": record.source_id,
        "source_record_id": record.source_record_id,
        "outcome": str(record.outcome),
        "title": record.asset.title,
        "year": record.asset.year,
        "asset_type": str(record.asset.asset_type),
        "domains": list(record.asset.domain_slugs),
        "external_identifiers": [
            {
                "scheme": str(identifier.scheme),
                "value": identifier.value,
            }
            for identifier in record.asset.external_identifiers
        ],
        "representation_count": len(record.representations),
        "relation_count": len(record.relations),
        # ``warnings`` remains the raw, canonical warning view. The other two
        # fields are a diagnostics-only operational projection.
        "warnings": [_warning_code(warning) for warning in record.warnings],
        "actionable_warnings": [
            _warning_code(warning) for warning in projection["actionable"]
        ],
        "ignored_warnings": projection["ignored"],
    }


def run_dry_run(
    sources: list[LoadedSource],
    *,
    known_domain_slugs: set[str] | None = None,
    retired_source_contracts: dict[str, dict[str, Any]] | None = None,
    operational_registry: SourceRegistry | None = None,
) -> dict[str, Any]:
    DEFAULT_REGISTRY.discover_entry_points()
    all_records: list[NormalizedRecord] = []
    source_reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    policy = (
        getattr(operational_registry, "operational_exclusions", None)
        if operational_registry is not None
        else None
    )
    roots = (
        {
            (item.source_id, item.root_key): item.path
            for item in operational_registry.describe()
        }
        if operational_registry is not None
        else {}
    )
    applied_rules: set[str] = set()
    unregistered_domains = unregistered_source_domains(
        sources,
        known_domain_slugs=known_domain_slugs,
    )
    if unregistered_domains:
        errors.append(
            {
                "source_id": "sources.yaml",
                "adapter": "taxonomy",
                "error_type": "UnregisteredDomain",
                "error": (
                    "Every source domain must be registered in taxonomy.yaml: "
                    f"{unregistered_domains}"
                ),
            }
        )

    retirement_contracts = retired_source_contracts or {}
    retired_sources_verified: list[str] = []
    historical_records_preserved = 0
    active_sources: list[str] = []
    for source in sources:
        try:
            retired_contract = retirement_contracts.get(source.source_id)
            if retired_contract is not None:
                expected_manifest = retired_contract.get("manifest_sha256")
                expected_config = retired_contract.get("source_config_sha256")
                actual_manifest = _sha256_file(source.manifest)
                actual_config = _json_digest(source.raw)
                if not isinstance(expected_manifest, str) or not isinstance(
                    expected_config, str
                ):
                    raise ValueError("retired source has no complete frozen contract")
                if actual_manifest != expected_manifest:
                    raise ValueError(
                        "retired source manifest digest changed: "
                        f"expected {expected_manifest}, got {actual_manifest}"
                    )
                if actual_config != expected_config:
                    raise ValueError(
                        "retired source configuration digest changed: "
                        f"expected {expected_config}, got {actual_config}"
                    )
                retired_sources_verified.append(source.source_id)
                historical_records_preserved += int(
                    retired_contract.get("historical_records_preserved", 0)
                )
                source_reports.append(
                    {
                        "source_id": source.source_id,
                        "adapter": source.adapter,
                        "lifecycle_status": "retired_verified",
                        "retirement_id": retired_contract.get("retirement_id"),
                        "expected_records": source.expected_records,
                        "normalized_records": 0,
                        "expected_count_matches": True,
                        "outcomes": {},
                        "warnings": {},
                        "representations": {},
                        "relations": {},
                        "sample": [],
                    }
                )
                continue
            active_sources.append(source.source_id)
            config = _source_config(source)
            adapter = DEFAULT_REGISTRY.create(
                config.adapter,
                config,
                base_dir=PROJECT_ROOT,
            )
            records = adapter.load()
            all_records.extend(records)
            outcomes = Counter(str(record.outcome) for record in records)
            warning_projections = [
                _warning_projection(
                    record,
                    policy=policy,
                    roots=roots,
                    applied_rules=applied_rules,
                )
                for record in records
            ]
            warning_counts = Counter(
                _warning_code(warning) for record in records for warning in record.warnings
            )
            actionable_warning_counts = Counter(
                _warning_code(warning)
                for projection in warning_projections
                for warning in projection["actionable"]
            )
            ignored_warnings = [
                warning
                for projection in warning_projections
                for warning in projection["ignored"]
            ]
            ignored_warning_counts = Counter(
                str(warning["code"]) for warning in ignored_warnings
            )
            representation_counts = Counter(
                str(representation.representation_type)
                for record in records
                for representation in record.representations
            )
            relation_counts = Counter(
                str(relation.relation_type) for record in records for relation in record.relations
            )
            source_reports.append(
                {
                    "source_id": source.source_id,
                    "adapter": source.adapter,
                    "lifecycle_status": "active",
                    "expected_records": source.expected_records,
                    "normalized_records": len(records),
                    "expected_count_matches": (
                        source.expected_records is None or source.expected_records == len(records)
                    ),
                    "outcomes": dict(sorted(outcomes.items())),
                    # ``warnings`` is intentionally raw; reconciliation and
                    # record fingerprints still see every canonical warning.
                    "warnings": dict(sorted(warning_counts.items())),
                    "actionable_warnings": dict(sorted(actionable_warning_counts.items())),
                    "ignored_warnings": dict(sorted(ignored_warning_counts.items())),
                    "ignored_warning_details": ignored_warnings,
                    "representations": dict(sorted(representation_counts.items())),
                    "relations": dict(sorted(relation_counts.items())),
                    "sample": [
                        _record_summary(record, warning_projection=projection)
                        for record, projection in zip(
                            records[:3], warning_projections[:3], strict=False
                        )
                    ],
                }
            )
        except Exception as exc:  # report one source without hiding other sources
            errors.append(
                {
                    "source_id": source.source_id,
                    "adapter": source.adapter,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    reconciliation = apply_weak_merge_approvals(reconcile_records(all_records))
    expected_total = sum(
        source.expected_records or 0
        for source in sources
        if source.source_id not in retirement_contracts
        and source.expected_records is not None
    )
    active_accounted_total = len(all_records) + len(errors)
    accounted_total = active_accounted_total + historical_records_preserved
    status = "passed"
    if errors or len(all_records) != expected_total:
        status = "failed"
    if reconciliation["duplicate_source_record_keys"]:
        status = "failed"
    if reconciliation["unapproved_review_groups"]:
        status = "failed"

    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "mode": "dry-run",
        "commit_performed": False,
        "source_roots_modified": False,
        "status": status,
        "active_sources": active_sources,
        "retired_sources_verified": retired_sources_verified,
        "historical_records_preserved": historical_records_preserved,
        "sources": source_reports,
        "totals": {
            "configured_sources": len(sources),
            "active_sources": len(active_sources),
            "retired_sources_verified": len(retired_sources_verified),
            "successful_sources": len(source_reports),
            "expected_source_records": expected_total,
            "normalized_records": len(all_records),
            "active_accounted_total": active_accounted_total,
            "historical_records_preserved": historical_records_preserved,
            "accounted_total": accounted_total,
            "representation_candidates": sum(len(record.representations) for record in all_records),
            "relation_candidates": sum(len(record.relations) for record in all_records),
            # The legacy field remains raw. The two companion fields make the
            # operational view explicit without altering canonical records.
            "warnings": sum(len(record.warnings) for record in all_records),
            "actionable_warnings": sum(
                sum(item["actionable_warnings"].values())
                for item in source_reports
                if item.get("lifecycle_status") == "active"
            ),
            "ignored_warnings": sum(
                sum(item["ignored_warnings"].values())
                for item in source_reports
                if item.get("lifecycle_status") == "active"
            ),
        },
        "reconciliation": reconciliation,
        "operational_path_exclusions": (
            policy.summary(applied_rules) if policy is not None else None
        ),
        "errors": errors,
        "next_step": (
            "Resolve every unapproved title/year merge candidate before database commit."
            if reconciliation["unapproved_review_groups"]
            else (
                "All weak merge candidates are explicitly approved; "
                "review the delta before commit."
            )
        ),
    }
