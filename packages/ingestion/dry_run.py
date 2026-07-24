from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from packages.adapters import DEFAULT_REGISTRY
from packages.cli.config import PROJECT_ROOT, LoadedSource, unregistered_source_domains
from packages.domain.schemas import NormalizedRecord, SourceConfig

from .reconciliation import apply_weak_merge_approvals, reconcile_records


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _record_summary(record: NormalizedRecord) -> dict[str, Any]:
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
        "warnings": [_warning_code(warning) for warning in record.warnings],
    }


def run_dry_run(
    sources: list[LoadedSource],
    *,
    known_domain_slugs: set[str] | None = None,
) -> dict[str, Any]:
    DEFAULT_REGISTRY.discover_entry_points()
    all_records: list[NormalizedRecord] = []
    source_reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
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

    for source in sources:
        try:
            config = _source_config(source)
            adapter = DEFAULT_REGISTRY.create(
                config.adapter,
                config,
                base_dir=PROJECT_ROOT,
            )
            records = adapter.load()
            all_records.extend(records)
            outcomes = Counter(str(record.outcome) for record in records)
            warning_counts = Counter(
                _warning_code(warning) for record in records for warning in record.warnings
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
                    "expected_records": source.expected_records,
                    "normalized_records": len(records),
                    "expected_count_matches": (
                        source.expected_records is None or source.expected_records == len(records)
                    ),
                    "outcomes": dict(sorted(outcomes.items())),
                    "warnings": dict(sorted(warning_counts.items())),
                    "representations": dict(sorted(representation_counts.items())),
                    "relations": dict(sorted(relation_counts.items())),
                    "sample": [_record_summary(record) for record in records[:3]],
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
        source.expected_records or 0 for source in sources if source.expected_records is not None
    )
    accounted_total = len(all_records) + len(errors)
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
        "sources": source_reports,
        "totals": {
            "configured_sources": len(sources),
            "successful_sources": len(source_reports),
            "expected_source_records": expected_total,
            "normalized_records": len(all_records),
            "accounted_total": accounted_total,
            "representation_candidates": sum(len(record.representations) for record in all_records),
            "relation_candidates": sum(len(record.relations) for record in all_records),
            "warnings": sum(len(record.warnings) for record in all_records),
        },
        "reconciliation": reconciliation,
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
