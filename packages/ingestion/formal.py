"""Transactional, idempotent import of adapter records into the operational store.

The importer deliberately never mutates a source root.  Every discovered file is
addressed through ``source_id + root_key + path_ref`` and the original manifest
record is retained as immutable provenance in ``source_record_versions``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from packages.adapters import DEFAULT_REGISTRY
from packages.cli.config import (
    PROJECT_ROOT,
    LoadedSource,
    load_domain_slugs,
    unregistered_source_domains,
)
from packages.domain.enums import (
    ActivityStatus,
    AuditAction,
    ConfirmationStatus,
    FieldClaimStatus,
    FieldResolutionKind,
    ImportStatus,
    MaterializationKind,
    OutboxStatus,
    RepresentationAvailability,
    SourceAssetRole,
)
from packages.domain.identifier_policy import RejectedIdentityKeys, rejected_identity_keys
from packages.domain.models import (
    Activity,
    Asset,
    AuditEvent,
    ExternalIdentifierModel,
    FieldClaim,
    FieldResolution,
    ImportRun,
    OutboxEvent,
    PaperProfile,
    Relation,
    RelationEvidenceModel,
    RepositoryProfile,
    Representation,
    RepresentationMount,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRecordVersion,
    SourceRetirement,
    SourceRetirementRollback,
    utc_now,
)
from packages.domain.schemas import (
    AssetReference,
    NormalizedRecord,
    RelationCandidate,
    RepresentationCandidate,
    RepresentationMountCandidate,
    SourceConfig,
)
from packages.domain.scope_policy import DomainScopePolicy

from .dry_run import _source_config
from .reconciliation import (
    UnapprovedWeakMergeError,
    apply_weak_merge_approvals,
    canonical_identifier,
    normalize_title,
    reconcile_records,
    record_fingerprint,
)
from .source_retirement import active_retirement_exists

IMPORTER_VERSION = "1.1"
_FROZEN_MANIFEST_SHA256 = "_frozen_manifest_sha256"
_FROZEN_SOURCE_CONFIG_SHA256 = "_frozen_source_config_sha256"
_FROZEN_AT = "_frozen_at"
_CLAIM_FIELDS = (
    "title",
    "title_zh",
    "subtitle",
    "authors",
    "year",
    "venue",
    "language",
    "abstract",
    "summary",
    "content",
    "analysis",
    "source_urls",
    "domain_slugs",
    "tags",
    "attributes",
)


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FormalImportValidationError(ValueError):
    """The complete normalized input does not satisfy a formal-import gate."""


class FrozenSourceChangedError(FormalImportValidationError):
    """A source contract or manifest changed after its first formal import."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_freeze_contract(source: LoadedSource) -> tuple[str, str]:
    """Return immutable manifest and source-config digests for one source."""

    return _sha256_file(source.manifest), _json_digest(source.raw)


def _verify_frozen_source_contract(model: Source, loaded: LoadedSource) -> tuple[str, str]:
    """Verify one configured source against its immutable database contract."""

    manifest_sha256, config_sha256 = _source_freeze_contract(loaded)
    config = dict(model.config or {})
    frozen_manifest = config.get(_FROZEN_MANIFEST_SHA256)
    frozen_config = config.get(_FROZEN_SOURCE_CONFIG_SHA256)
    if not isinstance(frozen_manifest, str) or not isinstance(frozen_config, str):
        raise FrozenSourceChangedError(
            f"Source {loaded.source_id} has no complete frozen source contract"
        )
    if frozen_manifest != manifest_sha256:
        raise FrozenSourceChangedError(
            f"Frozen manifest changed for source {loaded.source_id}: "
            f"expected {frozen_manifest}, got {manifest_sha256}"
        )
    if frozen_config != config_sha256:
        raise FrozenSourceChangedError(
            f"Frozen source configuration changed for source {loaded.source_id}: "
            f"expected {frozen_config}, got {config_sha256}"
        )
    return manifest_sha256, config_sha256


def retired_source_contracts_for_dry_run(
    session: Session,
    sources: Sequence[LoadedSource],
    *,
    require_complete: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return active-retirement contracts without normalizing retired sources."""

    loaded_aliases = {item.source_id for item in sources}
    retired_models = session.scalars(
        select(Source).where(active_retirement_exists(Source.id))
    ).all()
    unconfigured_retirements = sorted(
        model.alias for model in retired_models if model.alias not in loaded_aliases
    )
    if require_complete and unconfigured_retirements:
        raise FormalImportValidationError(
            "Active retired sources must remain in the complete source configuration: "
            f"{unconfigured_retirements}"
        )
    models = [model for model in retired_models if model.alias in loaded_aliases]
    contracts: dict[str, dict[str, Any]] = {}
    for model in models:
        if model.enabled:
            raise FormalImportValidationError(
                f"Source {model.alias} has an active retirement but remains enabled"
            )
        config = dict(model.config or {})
        rolled_back = select(SourceRetirementRollback.id).where(
            SourceRetirementRollback.retirement_id == SourceRetirement.id
        ).exists()
        retirement = session.scalar(
            select(SourceRetirement).where(
                SourceRetirement.source_id == model.id,
                ~rolled_back,
            )
        )
        if retirement is None:
            raise FormalImportValidationError(
                f"Source {model.alias} retirement state changed during validation"
            )
        if (
            retirement.frozen_manifest_sha256
            != config.get(_FROZEN_MANIFEST_SHA256)
            or retirement.frozen_source_config_sha256
            != config.get(_FROZEN_SOURCE_CONFIG_SHA256)
        ):
            raise FrozenSourceChangedError(
                f"Retirement evidence disagrees with frozen source {model.alias}"
            )
        contracts[model.alias] = {
            "manifest_sha256": config.get(_FROZEN_MANIFEST_SHA256),
            "source_config_sha256": config.get(_FROZEN_SOURCE_CONFIG_SHA256),
            "retirement_id": str(retirement.id),
            "historical_records_preserved": int(
                session.scalar(
                    select(func.count())
                    .select_from(SourceRecord)
                    .where(SourceRecord.source_id == model.id)
                )
                or 0
            ),
        }
    return contracts


def _partition_sources_for_import(
    session: Session,
    sources: Sequence[LoadedSource],
) -> tuple[list[LoadedSource], list[str], int]:
    """Separate active sources from signed retirements and verify the latter."""

    contracts = retired_source_contracts_for_dry_run(session, sources)
    active: list[LoadedSource] = []
    retired: list[str] = []
    historical_records = 0
    models = (
        {
            model.alias: model
            for model in session.scalars(
                select(Source).where(
                    Source.alias.in_([item.source_id for item in sources])
                )
            ).all()
        }
        if sources
        else {}
    )
    for loaded in sources:
        contract = contracts.get(loaded.source_id)
        if contract is None:
            active.append(loaded)
            continue
        model = models.get(loaded.source_id)
        if model is None:
            raise FormalImportValidationError(
                f"Retired source {loaded.source_id} has no database source row"
            )
        _verify_frozen_source_contract(model, loaded)
        retired.append(loaded.source_id)
        historical_records += int(contract["historical_records_preserved"])
    return active, retired, historical_records


@lru_cache(maxsize=4096)
def _sha256_file_cached(path_value: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    path = Path(path_value)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=1024)
def _directory_digest_cached(path_value: str, mtime_ns: int) -> str:
    """Return a cheap, deterministic tree fingerprint without reading file bodies."""

    del mtime_ns
    path = Path(path_value)
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
        try:
            stat = child.stat()
        except OSError:
            continue
        relative = child.relative_to(path).as_posix()
        kind = "d" if child.is_dir() else "f"
        digest.update(f"{kind}\0{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def canonical_repository_url(value: str) -> str:
    """Normalize a logical repository URL while preserving meaningful subpaths."""

    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized
    scheme = parsed.scheme.casefold()
    host = parsed.netloc.casefold()
    path = parsed.path.rstrip("/")
    return f"{scheme}://{host}{path}"


def _repository_title(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[-1] if parts else parsed.netloc


def _repository_profile(url: str) -> dict[str, str | None]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return {
        "host": parsed.netloc.casefold() or None,
        "owner": parts[0] if parts else None,
        "name": parts[1] if len(parts) > 1 else (parts[0] if parts else None),
    }


def _stable_union(values: Iterable[Iterable[str]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for items in values:
        for item in items:
            cleaned = str(item).strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                result.append(cleaned)
    return result


def _first(records: Sequence[NormalizedRecord], field_name: str) -> Any:
    for record in records:
        value = getattr(record.asset, field_name)
        if value not in (None, "", [], {}):
            return value
    return None


def _merged_mapping_field(
    records: Sequence[NormalizedRecord], field_name: str
) -> dict[str, Any]:
    """Merge mapping-valued claims without discarding independent enrichments.

    Paper metadata commonly arrives from an immutable catalog first and a later
    release-audit sidecar second.  Selecting only the first non-empty mapping made
    code/checkpoint evidence disappear even though both sources were retained as
    provenance.  A deterministic recursive merge keeps the catalog fields and lets
    the later source refine only the keys it explicitly owns.
    """

    def merge(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
        for key, value in incoming.items():
            normalized_key = str(key)
            existing = target.get(normalized_key)
            if isinstance(existing, Mapping) and isinstance(value, Mapping):
                nested = {str(item_key): item_value for item_key, item_value in existing.items()}
                merge(nested, value)
                target[normalized_key] = nested
            else:
                target[normalized_key] = value

    result: dict[str, Any] = {}
    for record in sorted(records, key=lambda item: (item.source_id, item.source_record_id)):
        value = getattr(record.asset, field_name)
        if isinstance(value, Mapping):
            merge(result, value)
    return result


def _merged_asset_values(records: Sequence[NormalizedRecord]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda item: (item.source_id, item.source_record_id))
    return {
        "asset_type": str(_first(ordered, "asset_type") or "paper"),
        "title": str(_first(ordered, "title") or "Untitled"),
        "title_zh": _first(ordered, "title_zh"),
        "subtitle": _first(ordered, "subtitle"),
        "authors": _stable_union(record.asset.authors for record in ordered),
        "year": _first(ordered, "year"),
        "venue": _first(ordered, "venue"),
        "language": _first(ordered, "language"),
        "abstract": _first(ordered, "abstract"),
        "summary": _first(ordered, "summary"),
        "content": _first(ordered, "content"),
        "analysis": _merged_mapping_field(ordered, "analysis"),
        "visibility": str(_first(ordered, "visibility") or "private"),
        "source_urls": _stable_union(
            [str(value) for value in record.asset.source_urls] for record in ordered
        ),
        "domain_slugs": _stable_union(record.asset.domain_slugs for record in ordered),
        "tags": _stable_union(record.asset.tags for record in ordered),
        "attributes": _merged_mapping_field(ordered, "attributes"),
    }


class _UnionFind:
    def __init__(self, keys: Iterable[str]) -> None:
        self.parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self.parent[key]
        if parent != key:
            self.parent[key] = self.find(parent)
        return self.parent[key]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def identity_groups(
    records: Sequence[NormalizedRecord],
    *,
    approved_weak_merges: Mapping[str, frozenset[str]] | None = None,
    rejected_keys: RejectedIdentityKeys = frozenset(),
) -> tuple[list[list[NormalizedRecord]], dict[str, Any]]:
    """Apply the approved strong and reviewed title/year merge decisions."""

    reconciliation = apply_weak_merge_approvals(
        reconcile_records(records, rejected_keys=rejected_keys),
        approvals=approved_weak_merges,
    )
    if reconciliation["unapproved_review_groups"]:
        raise UnapprovedWeakMergeError(reconciliation["unapproved_review_groups"])
    keyed = {f"{item.source_id}:{item.source_record_id}": item for item in records}
    union = _UnionFind(keyed)
    approved = [
        *reconciliation["auto_merge_groups"],
        *reconciliation["approved_review_groups"],
    ]
    for group in approved:
        keys = [
            f"{item['source_id']}:{item['source_record_id']}" for item in group["records"]
        ]
        for key in keys[1:]:
            union.union(keys[0], key)

    grouped: dict[str, list[NormalizedRecord]] = defaultdict(list)
    for key, record in keyed.items():
        grouped[union.find(key)].append(record)
    return (
        [
            sorted(items, key=lambda item: (item.source_id, item.source_record_id))
            for _, items in sorted(grouped.items())
        ],
        reconciliation,
    )


@dataclass(slots=True)
class ImportCounters:
    source_records_seen: int = 0
    source_records_created: int = 0
    source_record_versions_created: int = 0
    paper_assets_created: int = 0
    repository_assets_created: int = 0
    representations_created: int = 0
    representations_reused: int = 0
    representations_available: int = 0
    representations_missing: int = 0
    relations_created: int = 0
    relation_evidence_created: int = 0
    relation_candidates_resolved: int = 0
    relation_candidates_unresolved: int = 0
    field_claims_created: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            field_name: int(getattr(self, field_name))
            for field_name in self.__dataclass_fields__
        }


@dataclass(slots=True)
class ImportContext:
    session: Session
    sources: dict[str, LoadedSource]
    source_models: dict[str, Source]
    run: ImportRun
    domain_scope_policy: DomainScopePolicy
    rejected_keys: RejectedIdentityKeys = frozenset()
    counters: ImportCounters = field(default_factory=ImportCounters)
    record_models: dict[str, SourceRecord] = field(default_factory=dict)
    record_versions: dict[str, SourceRecordVersion] = field(default_factory=dict)
    record_assets: dict[str, Asset] = field(default_factory=dict)
    repository_assets: dict[str, Asset] = field(default_factory=dict)
    dirty_asset_ids: set[UUID] = field(default_factory=set)
    created_asset_ids: set[UUID] = field(default_factory=set)
    unresolved_relations: list[dict[str, Any]] = field(default_factory=list)


def _load_all_records(sources: Sequence[LoadedSource]) -> list[NormalizedRecord]:
    DEFAULT_REGISTRY.discover_entry_points()
    records: list[NormalizedRecord] = []
    for source in sources:
        config = _source_config(source)
        adapter = DEFAULT_REGISTRY.create(config.adapter, config, base_dir=PROJECT_ROOT)
        records.extend(adapter.load())
    return records


def _validate_loaded_records(
    sources: Sequence[LoadedSource],
    records: Sequence[NormalizedRecord],
    *,
    known_domain_slugs: set[str],
) -> None:
    """Fail closed before writes when adapter output violates the source contract."""

    configured = {source.source_id: source for source in sources}
    counts = Counter(record.source_id for record in records)
    unknown_sources = sorted(set(counts) - set(configured))
    if unknown_sources:
        raise FormalImportValidationError(
            "Adapters emitted records for undeclared sources: "
            f"{unknown_sources}"
        )

    count_errors: list[str] = []
    for source in sources:
        if source.expected_records is None:
            count_errors.append(f"{source.source_id}: expected_records is required")
            continue
        actual = counts[source.source_id]
        if actual != source.expected_records:
            count_errors.append(
                f"{source.source_id}: expected {source.expected_records}, got {actual}"
            )
    if count_errors:
        raise FormalImportValidationError(
            "Formal import record-count gate failed: " + "; ".join(count_errors)
        )

    missing_domains = sorted(
        {
            f"{record.source_id}:{record.source_record_id}"
            for record in records
            if not record.asset.domain_slugs
        }
    )
    if missing_domains:
        raise FormalImportValidationError(
            "Normalized records must declare at least one taxonomy domain: "
            f"{missing_domains}"
        )

    unregistered_domains = sorted(
        {
            domain
            for record in records
            for domain in record.asset.domain_slugs
            if domain not in known_domain_slugs
        }
    )
    if unregistered_domains:
        raise FormalImportValidationError(
            "Adapter output contains domains not registered in "
            f"config/taxonomy.yaml: {unregistered_domains}"
        )


def _persisted_source_path(path: Path) -> str:
    """Translate a container mount back to the host path used by the gateway."""

    container_root = os.environ.get("HUB_CONTAINER_WORKSPACE_ROOT")
    host_root = os.environ.get("HUB_HOST_WORKSPACE_ROOT")
    if not container_root or not host_root:
        return str(path)
    try:
        relative = path.resolve().relative_to(Path(container_root).resolve())
    except ValueError:
        return str(path)
    if len(host_root) >= 2 and host_root[1] == ":":
        return str(PureWindowsPath(host_root, *relative.parts))
    return str(Path(host_root) / relative)


def _upsert_sources(session: Session, sources: Sequence[LoadedSource]) -> dict[str, Source]:
    existing_models = {
        loaded.source_id: session.scalar(
            select(Source).where(Source.alias == loaded.source_id)
        )
        for loaded in sources
    }
    freeze_contracts: dict[str, tuple[str, str, str]] = {}
    frozen_at = utc_now().isoformat()
    for loaded in sources:
        manifest_sha256, config_sha256 = _source_freeze_contract(loaded)
        model = existing_models[loaded.source_id]
        existing_config = dict(model.config or {}) if model is not None else {}
        frozen_manifest = existing_config.get(_FROZEN_MANIFEST_SHA256)
        frozen_config = existing_config.get(_FROZEN_SOURCE_CONFIG_SHA256)
        if frozen_manifest is not None and frozen_manifest != manifest_sha256:
            raise FrozenSourceChangedError(
                f"Frozen manifest changed for source {loaded.source_id}: "
                f"expected {frozen_manifest}, got {manifest_sha256}"
            )
        if frozen_config is not None and frozen_config != config_sha256:
            raise FrozenSourceChangedError(
                f"Frozen source configuration changed for source {loaded.source_id}: "
                f"expected {frozen_config}, got {config_sha256}"
            )
        freeze_contracts[loaded.source_id] = (
            manifest_sha256,
            config_sha256,
            str(existing_config.get(_FROZEN_AT) or frozen_at),
        )

    result: dict[str, Source] = {}
    for loaded in sources:
        model = existing_models[loaded.source_id]
        config = dict(loaded.raw)
        config["asset_roots"] = dict(config.get("asset_roots") or {})
        manifest_sha256, config_sha256, source_frozen_at = freeze_contracts[
            loaded.source_id
        ]
        config[_FROZEN_MANIFEST_SHA256] = manifest_sha256
        config[_FROZEN_SOURCE_CONFIG_SHA256] = config_sha256
        config[_FROZEN_AT] = source_frozen_at
        if model is None:
            model = Source(
                alias=loaded.source_id,
                label=loaded.label,
                adapter=loaded.adapter,
                root_path=_persisted_source_path(loaded.root),
                manifest_path=_persisted_source_path(loaded.manifest),
                read_only=True,
                enabled=True,
                config=config,
            )
            session.add(model)
            session.flush()
        else:
            model.label = loaded.label
            model.adapter = loaded.adapter
            model.root_path = _persisted_source_path(loaded.root)
            model.manifest_path = _persisted_source_path(loaded.manifest)
            model.read_only = True
            # Enabled is lifecycle state. Normal ingestion must never resurrect a
            # disabled source while refreshing its non-lifecycle fields.
            model.config = config
        result[loaded.source_id] = model
    return result


def _existing_asset_for_group(
    session: Session,
    source_models: dict[str, Source],
    records: Sequence[NormalizedRecord],
) -> Asset | None:
    conditions = [
        (source_models[record.source_id].id, record.source_record_id) for record in records
    ]
    assets: dict[UUID, Asset] = {}
    for source_id, record_id in conditions:
        existing = session.scalar(
            select(SourceRecord).where(
                SourceRecord.source_id == source_id,
                SourceRecord.source_record_id == record_id,
            )
        )
        if existing is not None and existing.asset_id is not None:
            asset = session.get(Asset, existing.asset_id)
            if asset is not None:
                assets[asset.id] = asset
    if not assets:
        return None
    # A previous importer may have created separate records before a merge rule was
    # approved.  Deterministically retain the smallest UUID and leave redirect
    # creation to the explicit merge service; normal fresh imports have one asset.
    return assets[min(assets, key=str)]


def _apply_asset_values(
    session: Session,
    asset: Asset,
    values: dict[str, Any],
) -> None:
    for name, value in values.items():
        setattr(asset, name, value)
    overrides = session.scalars(
        select(FieldResolution).where(
            FieldResolution.entity_type == "asset",
            FieldResolution.entity_id == asset.id,
            FieldResolution.resolution_kind == FieldResolutionKind.OVERRIDE.value,
        )
    ).all()
    for resolution in overrides:
        field_name = resolution.field_path.removeprefix("/")
        if "/" not in field_name and field_name in _CLAIM_FIELDS:
            setattr(asset, field_name, resolution.override_value)
    asset.deleted_at = None


def _upsert_paper_group(ctx: ImportContext, records: Sequence[NormalizedRecord]) -> Asset:
    values = _merged_asset_values(records)
    asset = _existing_asset_for_group(ctx.session, ctx.source_models, records)
    values["domain_slugs"] = ctx.domain_scope_policy.filter_domains(
        values["domain_slugs"],
        asset_id=asset.id if asset is not None else None,
        title=str(values["title"]),
    )
    if asset is None:
        asset = Asset(**values)
        ctx.session.add(asset)
        ctx.session.flush()
        if asset.asset_type == "paper":
            ctx.counters.paper_assets_created += 1
        ctx.created_asset_ids.add(asset.id)
        ctx.dirty_asset_ids.add(asset.id)
    else:
        before = {
            name: _json_digest(getattr(asset, name))
            for name in (*values.keys(), "deleted_at")
        }
        _apply_asset_values(ctx.session, asset, values)
        after = {
            name: _json_digest(getattr(asset, name))
            for name in (*values.keys(), "deleted_at")
        }
        if before != after:
            ctx.dirty_asset_ids.add(asset.id)

    if asset.asset_type == "paper":
        profile = ctx.session.get(PaperProfile, asset.id)
        if profile is None:
            ctx.session.add(PaperProfile(asset_id=asset.id))
            ctx.dirty_asset_ids.add(asset.id)

    for record in records:
        key = f"{record.source_id}:{record.source_record_id}"
        ctx.record_assets[key] = asset
        for identifier in record.asset.external_identifiers:
            canonical = identifier.canonical or canonical_identifier(
                identifier.scheme, identifier.value
            ).split(":", 1)[-1]
            existing = ctx.session.scalar(
                select(ExternalIdentifierModel).where(
                    ExternalIdentifierModel.scheme == identifier.scheme,
                    ExternalIdentifierModel.scope == identifier.scope,
                    ExternalIdentifierModel.canonical == canonical,
                    ExternalIdentifierModel.asset_id == asset.id,
                )
            )
            if existing is None:
                ctx.session.add(
                    ExternalIdentifierModel(
                        asset_id=asset.id,
                        scheme=identifier.scheme,
                        scope=identifier.scope,
                        value=identifier.value,
                        canonical=canonical,
                        is_identity=bool(identifier.is_identity)
                        and (
                            record.source_id,
                            record.source_record_id,
                            identifier.scheme,
                            identifier.scope,
                            canonical,
                        )
                        not in ctx.rejected_keys,
                    )
                )
                ctx.dirty_asset_ids.add(asset.id)
    return asset


def _upsert_source_record(
    ctx: ImportContext,
    record: NormalizedRecord,
    asset: Asset,
) -> tuple[SourceRecord, SourceRecordVersion]:
    key = f"{record.source_id}:{record.source_record_id}"
    source = ctx.source_models[record.source_id]
    ctx.counters.source_records_seen += 1
    model = ctx.session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_id == source.id,
            SourceRecord.source_record_id == record.source_record_id,
        )
    )
    normalized = record.model_dump(mode="json", by_alias=False)
    normalized_digest = record_fingerprint(record)
    if model is None:
        model = SourceRecord(
            source_id=source.id,
            source_record_id=record.source_record_id,
            asset_id=asset.id,
            import_run_id=ctx.run.id,
            outcome=str(record.outcome),
            raw_data=dict(record.raw),
            source_fields=dict(record.source_fields),
            normalized_digest=normalized_digest,
            warnings=[warning.model_dump(mode="json") for warning in record.warnings],
        )
        ctx.session.add(model)
        ctx.session.flush()
        ctx.counters.source_records_created += 1
    else:
        model.asset_id = asset.id
        model.import_run_id = ctx.run.id
        model.outcome = str(record.outcome)
        model.raw_data = dict(record.raw)
        model.source_fields = dict(record.source_fields)
        model.normalized_digest = normalized_digest
        model.warnings = [warning.model_dump(mode="json") for warning in record.warnings]
        model.last_seen_at = utc_now()

    version = ctx.session.scalar(
        select(SourceRecordVersion).where(
            SourceRecordVersion.source_record_id == model.id,
            SourceRecordVersion.raw_digest == normalized_digest,
        )
    )
    if version is None:
        version = SourceRecordVersion(
            source_record_id=model.id,
            import_run_id=ctx.run.id,
            adapter_version=record.adapter_version,
            raw_data=normalized,
            source_fields=dict(record.source_fields),
            raw_digest=normalized_digest,
        )
        ctx.session.add(version)
        ctx.session.flush()
        ctx.counters.source_record_versions_created += 1

    link = ctx.session.scalar(
        select(SourceRecordAsset).where(
            SourceRecordAsset.source_record_id == model.id,
            SourceRecordAsset.asset_id == asset.id,
            SourceRecordAsset.role == SourceAssetRole.PRIMARY.value,
        )
    )
    if link is None:
        ctx.session.add(
            SourceRecordAsset(
                source_record_id=model.id,
                asset_id=asset.id,
                source_record_version_id=version.id,
                role=SourceAssetRole.PRIMARY.value,
                evidence={"import_run_id": str(ctx.run.id)},
            )
        )
    else:
        link.source_record_version_id = version.id
        link.deleted_at = None
    ctx.record_models[key] = model
    ctx.record_versions[key] = version
    return model, version


def _dotted_path_to_pointer(value: str) -> str:
    components = value.split(".")
    encoded = [
        component.replace("~", "~0").replace("/", "~1")
        for component in components
        if component
    ]
    return "/" + "/".join(encoded)


def _field_source_pointer(record: NormalizedRecord, field_name: str) -> str | None:
    """Resolve a claim to the real upstream field declared by the adapter."""

    declared = record.source_fields.get("field_pointers")
    if isinstance(declared, Mapping):
        locator = declared.get(field_name)
        if isinstance(locator, str) and locator.strip():
            pointer = locator.strip()
            if not pointer.startswith("/"):
                pointer = _dotted_path_to_pointer(pointer)
            return f"{record.source_path}#{pointer}" if record.source_path else pointer
        if isinstance(locator, Mapping):
            raw_pointer = locator.get("json_pointer") or locator.get("pointer")
            raw_path = locator.get("input_path") or record.source_path
            if isinstance(raw_pointer, str) and raw_pointer.strip():
                pointer = raw_pointer.strip()
                if not pointer.startswith("/"):
                    pointer = _dotted_path_to_pointer(pointer)
                if isinstance(raw_path, str) and raw_path.strip():
                    return f"{raw_path.strip()}#{pointer}"
                return pointer

    mapping = record.source_fields.get("field_mapping")
    if isinstance(mapping, Mapping):
        raw_path = mapping.get(field_name)
        if isinstance(raw_path, str) and raw_path.strip():
            pointer = _dotted_path_to_pointer(raw_path.strip())
            return f"{record.source_path}#{pointer}" if record.source_path else pointer
    return record.source_path


def _write_claims(
    ctx: ImportContext,
    record: NormalizedRecord,
    asset: Asset,
    version: SourceRecordVersion,
) -> None:
    asset_payload = record.asset.model_dump(mode="json")
    for field_name in _CLAIM_FIELDS:
        value = asset_payload.get(field_name)
        if value in (None, "", [], {}):
            continue
        field_path = f"/{field_name}"
        value_digest = _json_digest(value)
        source_key = f"{record.source_id}:{record.source_record_id}"
        source_pointer = _field_source_pointer(record, field_name)
        claim = ctx.session.scalar(
            select(FieldClaim).where(
                FieldClaim.entity_type == "asset",
                FieldClaim.entity_id == asset.id,
                FieldClaim.field_path == field_path,
                FieldClaim.value_digest == value_digest,
                FieldClaim.source_key == source_key,
            )
        )
        if claim is None:
            claim = FieldClaim(
                entity_type="asset",
                entity_id=asset.id,
                field_path=field_path,
                value_json=value,
                value_digest=value_digest,
                source_record_version_id=version.id,
                provider=record.adapter_name,
                source_pointer=source_pointer,
                source_key=source_key,
                authority=100,
                confidence=1.0,
                status=FieldClaimStatus.ACTIVE.value,
            )
            ctx.session.add(claim)
            ctx.session.flush()
            ctx.counters.field_claims_created += 1
        else:
            before_provenance = (
                claim.source_record_version_id,
                claim.provider,
                claim.source_pointer,
            )
            claim.source_record_version_id = version.id
            claim.provider = record.adapter_name
            claim.source_pointer = source_pointer
            after_provenance = (
                claim.source_record_version_id,
                claim.provider,
                claim.source_pointer,
            )
            if before_provenance != after_provenance:
                ctx.dirty_asset_ids.add(asset.id)

        resolved_value = getattr(asset, field_name)
        if _json_digest(resolved_value) != value_digest:
            continue
        resolution = ctx.session.scalar(
            select(FieldResolution).where(
                FieldResolution.entity_type == "asset",
                FieldResolution.entity_id == asset.id,
                FieldResolution.field_path == field_path,
            )
        )
        if resolution is None:
            ctx.session.add(
                FieldResolution(
                    entity_type="asset",
                    entity_id=asset.id,
                    field_path=field_path,
                    resolution_kind=FieldResolutionKind.CLAIM.value,
                    selected_claim_id=claim.id,
                    reason="deterministic import resolution",
                    resolved_by="importer",
                )
            )


def _representation_path(
    loaded: LoadedSource,
    config: SourceConfig,
    candidate: RepresentationCandidate,
) -> Path | None:
    if candidate.path_ref is None:
        return None
    root_value = config.asset_roots.get(candidate.root_key)
    root = (loaded.root / root_value).resolve() if root_value else loaded.root.resolve()
    candidate_path = (root / candidate.path_ref).resolve()
    try:
        candidate_path.relative_to(root)
    except ValueError:
        return None
    return candidate_path


def _representation_state(
    path: Path | None,
    candidate: RepresentationCandidate,
    existing: Representation | None,
) -> tuple[
    str,
    str,
    str | None,
    int | None,
    str | None,
    dict[str, Any],
    bool,
]:
    attributes = dict(candidate.metadata)
    if path is None:
        return (
            RepresentationAvailability.AVAILABLE.value
            if candidate.url is not None
            else RepresentationAvailability.MISSING.value,
            MaterializationKind.URL.value,
            candidate.sha256,
            candidate.size_bytes,
            candidate.mime_type,
            attributes,
            existing is not None,
        )
    if not path.exists():
        attributes["_fingerprint_strategy"] = "missing"
        return (
            RepresentationAvailability.MISSING.value,
            str(candidate.materialization_kind),
            candidate.sha256,
            candidate.size_bytes,
            candidate.mime_type,
            attributes,
            existing is not None,
        )
    stat = path.stat()
    attributes["_source_mtime_ns"] = stat.st_mtime_ns
    attributes["_source_size"] = stat.st_size
    if path.is_dir():
        cached = (
            existing is not None
            and existing.tree_digest is not None
            and (
                (
                    candidate.revision is not None
                    and existing.revision == candidate.revision
                )
                or (
                    existing.attributes.get("_source_mtime_ns") == stat.st_mtime_ns
                    and existing.attributes.get("_source_size") == stat.st_size
                )
            )
        )
        digest = (
            existing.tree_digest
            if cached and existing is not None
            else candidate.tree_digest
            or _directory_digest_cached(str(path), stat.st_mtime_ns)
        )
        attributes["_fingerprint_strategy"] = (
            "revision-or-stat-cache" if cached else "directory-tree-stat"
        )
        materialization = (
            MaterializationKind.BUNDLE.value
            if candidate.materialization_kind == MaterializationKind.BUNDLE
            else MaterializationKind.DIRECTORY.value
        )
        return (
            RepresentationAvailability.AVAILABLE.value,
            materialization,
            digest,
            None,
            candidate.mime_type,
            attributes,
            cached,
        )
    cached = (
        existing is not None
        and existing.sha256 is not None
        and existing.attributes.get("_source_mtime_ns") == stat.st_mtime_ns
        and existing.attributes.get("_source_size") == stat.st_size
    )
    digest = (
        existing.sha256
        if cached and existing is not None
        else candidate.sha256
        or _sha256_file_cached(str(path), stat.st_size, stat.st_mtime_ns)
    )
    attributes["_fingerprint_strategy"] = "size-mtime-cache" if cached else "sha256"
    return (
        RepresentationAvailability.AVAILABLE.value,
        MaterializationKind.FILE.value,
        digest,
        stat.st_size,
        candidate.mime_type or mimetypes.guess_type(path.name)[0],
        attributes,
        cached,
    )


def _upsert_repository(
    ctx: ImportContext,
    record: NormalizedRecord,
    candidate: RepresentationCandidate,
) -> Asset:
    assert candidate.url is not None
    canonical_url = canonical_repository_url(str(candidate.url))
    cached = ctx.repository_assets.get(canonical_url)
    if cached is not None:
        profile = ctx.session.get(RepositoryProfile, cached.id)
        assert profile is not None
        _merge_repository_provenance(ctx, cached, profile, record, candidate)
        return cached
    profile = ctx.session.scalar(
        select(RepositoryProfile).where(RepositoryProfile.canonical_url == canonical_url)
    )
    if profile is not None:
        asset = ctx.session.get(Asset, profile.asset_id)
        assert asset is not None
        _merge_repository_provenance(ctx, asset, profile, record, candidate)
        ctx.repository_assets[canonical_url] = asset
        return asset

    details = _repository_profile(canonical_url)
    repository_title = _repository_title(canonical_url)
    repository_domains = ctx.domain_scope_policy.filter_domains(
        record.asset.domain_slugs,
        asset_id=None,
        title=record.asset.title,
    )
    asset = Asset(
        asset_type="repository",
        title=repository_title,
        authors=[],
        visibility="private",
        source_urls=[canonical_url],
        domain_slugs=repository_domains,
        tags=[],
        analysis={},
        attributes={"logical_repository": True},
    )
    ctx.session.add(asset)
    ctx.session.flush()
    repository_attributes = dict(candidate.metadata)
    repository_attributes["source_metadata"] = {
        f"{record.source_id}:{record.source_record_id}": dict(candidate.metadata)
    }
    ctx.session.add(
        RepositoryProfile(
            asset_id=asset.id,
            canonical_url=canonical_url,
            host=details["host"],
            owner=details["owner"],
            name=details["name"],
            pinned_revision=candidate.revision,
            license_spdx=str(candidate.metadata.get("license") or "") or None,
            code_status=str(candidate.metadata.get("status") or "unknown"),
            attributes=repository_attributes,
        )
    )
    scheme = "github" if details["host"] == "github.com" else "url"
    ctx.session.add(
        ExternalIdentifierModel(
            asset_id=asset.id,
            scheme=scheme,
            scope="",
            value=canonical_url,
            canonical=canonical_url,
            is_identity=True,
        )
    )
    ctx.repository_assets[canonical_url] = asset
    ctx.created_asset_ids.add(asset.id)
    ctx.dirty_asset_ids.add(asset.id)
    ctx.counters.repository_assets_created += 1
    return asset


def _merge_repository_provenance(
    ctx: ImportContext,
    asset: Asset,
    profile: RepositoryProfile,
    record: NormalizedRecord,
    candidate: RepresentationCandidate,
) -> None:
    """Preserve all source domains and per-record repository metadata on reuse."""

    before = _json_digest(
        {
            "domains": asset.domain_slugs,
            "source_urls": asset.source_urls,
            "pinned_revision": profile.pinned_revision,
            "license_spdx": profile.license_spdx,
            "code_status": profile.code_status,
            "attributes": profile.attributes,
        }
    )
    canonical_url = canonical_repository_url(str(candidate.url))
    asset.domain_slugs = ctx.domain_scope_policy.filter_domains(
        _stable_union((asset.domain_slugs or [], record.asset.domain_slugs)),
        asset_id=asset.id,
        title=record.asset.title,
    )
    asset.source_urls = _stable_union((asset.source_urls or [], [canonical_url]))

    if profile.pinned_revision is None and candidate.revision:
        profile.pinned_revision = candidate.revision
    candidate_license = str(candidate.metadata.get("license") or "").strip()
    if candidate_license and not profile.license_spdx:
        profile.license_spdx = candidate_license
    candidate_status = str(candidate.metadata.get("status") or "").strip()
    if candidate_status and profile.code_status in {"", "unknown"}:
        profile.code_status = candidate_status

    attributes = dict(profile.attributes or {})
    source_metadata = dict(attributes.get("source_metadata") or {})
    source_metadata[f"{record.source_id}:{record.source_record_id}"] = dict(candidate.metadata)
    attributes["source_metadata"] = source_metadata
    profile.attributes = attributes
    after = _json_digest(
        {
            "domains": asset.domain_slugs,
            "source_urls": asset.source_urls,
            "pinned_revision": profile.pinned_revision,
            "license_spdx": profile.license_spdx,
            "code_status": profile.code_status,
            "attributes": profile.attributes,
        }
    )
    if before != after:
        ctx.dirty_asset_ids.add(asset.id)


def _representation_query(
    source_id: UUID,
    asset_id: UUID,
    candidate: RepresentationCandidate,
) -> Select[tuple[Representation]]:
    query = select(Representation).where(
        Representation.asset_id == asset_id,
        Representation.source_id == source_id,
        Representation.root_key == candidate.root_key,
        Representation.role == str(candidate.role),
    )
    if candidate.path_ref is not None:
        return query.where(Representation.path_ref == candidate.path_ref)
    return query.where(
        Representation.path_ref.is_(None),
        Representation.external_url == (str(candidate.url) if candidate.url else None),
    )


def _upsert_representation(
    ctx: ImportContext,
    record: NormalizedRecord,
    candidate: RepresentationCandidate,
    paper: Asset,
) -> tuple[Representation, Asset]:
    source_model = ctx.source_models[record.source_id]
    source_record = ctx.record_models[f"{record.source_id}:{record.source_record_id}"]
    loaded = ctx.sources[record.source_id]
    config = _source_config(loaded)

    target = paper
    if str(candidate.representation_type) == "repository" and candidate.url is not None:
        target = _upsert_repository(ctx, record, candidate)
        link = ctx.session.scalar(
            select(SourceRecordAsset).where(
                SourceRecordAsset.source_record_id == source_record.id,
                SourceRecordAsset.asset_id == target.id,
                SourceRecordAsset.role == SourceAssetRole.REPOSITORY.value,
            )
        )
        if link is None:
            ctx.session.add(
                SourceRecordAsset(
                    source_record_id=source_record.id,
                    asset_id=target.id,
                    source_record_version_id=ctx.record_versions[
                        f"{record.source_id}:{record.source_record_id}"
                    ].id,
                    role=SourceAssetRole.REPOSITORY.value,
                    evidence={"repository_url": str(candidate.url)},
                )
            )

    model = ctx.session.scalar(
        _representation_query(source_model.id, target.id, candidate)
    )
    path = _representation_path(loaded, config, candidate)
    (
        availability,
        materialization,
        digest,
        size,
        mime_type,
        representation_attributes,
        fingerprint_reused,
    ) = _representation_state(path, candidate, model)
    directory_materializations = {
        MaterializationKind.DIRECTORY.value,
        MaterializationKind.BUNDLE.value,
    }
    if model is None:
        model = Representation(
            asset_id=target.id,
            source_id=source_model.id,
            source_record_id=source_record.id,
            representation_type=str(candidate.representation_type),
            role=str(candidate.role),
            materialization_kind=materialization,
            availability=availability,
            root_key=candidate.root_key,
            path_ref=candidate.path_ref,
            external_url=str(candidate.url) if candidate.url else None,
            entrypoint=candidate.entrypoint,
            revision=candidate.revision,
            tree_digest=(
                digest if materialization in directory_materializations else None
            ),
            sha256=digest if materialization not in directory_materializations else None,
            mime_type=mime_type,
            size_bytes=size,
            network_required=candidate.network_required,
            attributes=representation_attributes,
            verified_at=utc_now(),
        )
        ctx.session.add(model)
        ctx.session.flush()
        ctx.counters.representations_created += 1
        ctx.dirty_asset_ids.add(target.id)
    else:
        before = (
            model.asset_id,
            model.source_record_id,
            model.representation_type,
            model.materialization_kind,
            model.availability,
            model.external_url,
            model.entrypoint,
            model.revision,
            model.tree_digest,
            model.sha256,
            model.mime_type,
            model.size_bytes,
            model.network_required,
            model.deleted_at,
        )
        existing_attributes = dict(model.attributes or {})
        for derived_key in (
            "sandbox_policy",
            "sandbox_policy_source",
            "sandbox_policy_content_sha256",
        ):
            if derived_key in existing_attributes:
                representation_attributes[derived_key] = existing_attributes[derived_key]
        persisted_policy = str(
            representation_attributes.get("sandbox_policy") or ""
        )
        model.asset_id = target.id
        # A logical repository can be referenced by several papers in the same
        # source. Keep the representation's original provenance owner stable;
        # SourceRecordAsset links preserve every additional paper association.
        if model.source_record_id is None:
            model.source_record_id = source_record.id
        model.representation_type = str(candidate.representation_type)
        model.materialization_kind = materialization
        model.availability = availability
        model.external_url = str(candidate.url) if candidate.url else None
        model.entrypoint = candidate.entrypoint
        model.revision = candidate.revision
        model.tree_digest = (
            digest if materialization in directory_materializations else None
        )
        model.sha256 = digest if materialization not in directory_materializations else None
        model.mime_type = mime_type
        model.size_bytes = size
        model.network_required = (
            candidate.network_required or persisted_policy == "network_required"
        )
        model.attributes = representation_attributes
        model.verified_at = utc_now()
        model.deleted_at = None
        after = (
            model.asset_id,
            model.source_record_id,
            model.representation_type,
            model.materialization_kind,
            model.availability,
            model.external_url,
            model.entrypoint,
            model.revision,
            model.tree_digest,
            model.sha256,
            model.mime_type,
            model.size_bytes,
            model.network_required,
            model.deleted_at,
        )
        if before != after:
            ctx.dirty_asset_ids.add(target.id)
    if fingerprint_reused:
        ctx.counters.representations_reused += 1

    if availability == RepresentationAvailability.AVAILABLE.value:
        ctx.counters.representations_available += 1
    elif availability == RepresentationAvailability.MISSING.value:
        ctx.counters.representations_missing += 1

    mount_candidates = list(candidate.mounts)
    if candidate.path_ref is not None and not any(
        mount.mount_path == "/" for mount in mount_candidates
    ):
        mount_candidates.insert(
            0,
            RepresentationMountCandidate(
                source_id=record.source_id,
                root_key=candidate.root_key,
                path_ref=candidate.path_ref,
            ),
        )
    for mount_candidate in mount_candidates:
        mount_source = ctx.source_models.get(mount_candidate.source_id)
        if mount_source is None:
            raise FormalImportValidationError(
                "Representation mount references an unloaded source: "
                f"{mount_candidate.source_id}"
            )
        mount = ctx.session.scalar(
            select(RepresentationMount).where(
                RepresentationMount.representation_id == model.id,
                RepresentationMount.mount_path == mount_candidate.mount_path,
            )
        )
        if mount is None:
            ctx.session.add(
                RepresentationMount(
                    representation_id=model.id,
                    mount_path=mount_candidate.mount_path,
                    source_id=mount_source.id,
                    root_key=mount_candidate.root_key,
                    path_ref=mount_candidate.path_ref,
                    read_only=mount_candidate.read_only,
                    priority=mount_candidate.priority,
                )
            )
            ctx.dirty_asset_ids.add(target.id)
        else:
            before_mount = (
                mount.source_id,
                mount.root_key,
                mount.path_ref,
                mount.read_only,
                mount.priority,
                mount.deleted_at,
            )
            mount.source_id = mount_source.id
            mount.root_key = mount_candidate.root_key
            mount.path_ref = mount_candidate.path_ref
            mount.read_only = mount_candidate.read_only
            mount.priority = mount_candidate.priority
            mount.deleted_at = None
            after_mount = (
                mount.source_id,
                mount.root_key,
                mount.path_ref,
                mount.read_only,
                mount.priority,
                mount.deleted_at,
            )
            if before_mount != after_mount:
                ctx.dirty_asset_ids.add(target.id)
    return model, target


def _resolve_reference(
    ctx: ImportContext,
    reference: AssetReference,
    default_source: str,
) -> Asset | None:
    if reference.asset_id is not None:
        return ctx.session.get(Asset, reference.asset_id)
    if reference.source_record_id is not None:
        source_id = reference.source_id or default_source
        return ctx.record_assets.get(f"{source_id}:{reference.source_record_id}")
    identifier = reference.external_identifier
    if identifier is None:
        return None
    if identifier.scheme in {"github", "url"}:
        repository_url = canonical_repository_url(
            identifier.canonical or identifier.value
        )
        return ctx.repository_assets.get(repository_url)
    if identifier.scheme == "source":
        value = identifier.canonical or identifier.value
        prefix = f"{default_source}:label:"
        if value.startswith(prefix):
            label = normalize_title(value[len(prefix) :])
            candidates = [
                asset
                for key, asset in ctx.record_assets.items()
                if key.startswith(f"{default_source}:")
                and label in {normalize_title(asset.title), normalize_title(asset.title_zh or "")}
            ]
            unique = {asset.id: asset for asset in candidates}
            if len(unique) == 1:
                return next(iter(unique.values()))
    model = ctx.session.scalar(
        select(ExternalIdentifierModel).where(
            ExternalIdentifierModel.scheme == identifier.scheme,
            ExternalIdentifierModel.scope == identifier.scope,
            ExternalIdentifierModel.canonical == identifier.canonical,
            ExternalIdentifierModel.is_identity.is_(True),
        )
    )
    return ctx.session.get(Asset, model.asset_id) if model is not None else None


def _candidate_payload(candidate: RelationCandidate) -> dict[str, Any]:
    return candidate.model_dump(mode="json", by_alias=False)


def _upsert_relation(
    ctx: ImportContext,
    record: NormalizedRecord,
    candidate: RelationCandidate,
    index: int,
) -> None:
    paper = ctx.record_assets[f"{record.source_id}:{record.source_record_id}"]
    subject = (
        _resolve_reference(ctx, candidate.subject, record.source_id)
        if candidate.subject is not None
        else paper
    )
    target = _resolve_reference(ctx, candidate.object, record.source_id)
    outcome = {
        "source_id": record.source_id,
        "source_record_id": record.source_record_id,
        "candidate_index": index,
        "relation_type": str(candidate.relation_type),
    }
    if subject is None or target is None or subject.id == target.id:
        outcome["outcome"] = "unresolved" if target is None else "self_edge_rejected"
        outcome["candidate"] = _candidate_payload(candidate)
        ctx.unresolved_relations.append(outcome)
        ctx.counters.relation_candidates_unresolved += 1
        return

    relation = ctx.session.scalar(
        select(Relation).where(
            Relation.subject_asset_id == subject.id,
            Relation.relation_type == str(candidate.relation_type),
            Relation.object_asset_id == target.id,
            Relation.source_key == "canonical",
        )
    )
    if relation is None:
        relation = Relation(
            subject_asset_id=subject.id,
            object_asset_id=target.id,
            relation_type=str(candidate.relation_type),
            source_key="canonical",
            evidence={},
            confidence=candidate.confidence,
            confirmation_status=(
                ConfirmationStatus.CONFIRMED.value
                if candidate.confirmed
                else ConfirmationStatus.CANDIDATE.value
            ),
            attributes=dict(candidate.metadata),
        )
        ctx.session.add(relation)
        ctx.session.flush()
        ctx.counters.relations_created += 1
        ctx.dirty_asset_ids.update((subject.id, target.id))
    elif (
        candidate.confirmed
        and relation.confirmation_status == ConfirmationStatus.CANDIDATE.value
    ):
        relation.confirmation_status = ConfirmationStatus.CONFIRMED.value
        ctx.dirty_asset_ids.update((subject.id, target.id))

    evidence = candidate.evidence.model_dump(mode="json")
    evidence_hash = _json_digest(
        {
            "record": f"{record.source_id}:{record.source_record_id}",
            "relation": _candidate_payload(candidate),
        }
    )
    existing_evidence = ctx.session.scalar(
        select(RelationEvidenceModel).where(
            RelationEvidenceModel.relation_id == relation.id,
            RelationEvidenceModel.evidence_hash == evidence_hash,
        )
    )
    if existing_evidence is None:
        ctx.session.add(
            RelationEvidenceModel(
                relation_id=relation.id,
                source_record_version_id=ctx.record_versions[
                    f"{record.source_id}:{record.source_record_id}"
                ].id,
                provider=record.adapter_name,
                source_key=f"{record.source_id}:{record.source_record_id}:{index}",
                evidence=evidence,
                evidence_hash=evidence_hash,
                confidence=candidate.confidence,
            )
        )
        ctx.counters.relation_evidence_created += 1
    ctx.counters.relation_candidates_resolved += 1


def _emit_dirty_asset_events(ctx: ImportContext) -> None:
    for asset_id in sorted(ctx.dirty_asset_ids, key=str):
        asset = ctx.session.get(Asset, asset_id)
        if asset is None:
            continue
        created = asset_id in ctx.created_asset_ids
        ctx.session.add(
            AuditEvent(
                entity_type="asset",
                entity_id=asset.id,
                action=AuditAction.IMPORT.value,
                actor="importer",
                after={
                    "asset_type": asset.asset_type,
                    "title": asset.title,
                    "created": created,
                },
                details={"import_run_id": str(ctx.run.id)},
            )
        )
        ctx.session.add(
            OutboxEvent(
                aggregate_type="asset",
                aggregate_id=asset.id,
                event_type="asset.imported",
                payload={"asset_id": str(asset.id), "created": created},
                status=OutboxStatus.PENDING.value,
                dedupe_key=f"import:{ctx.run.id}:asset:{asset.id}",
            )
        )


def run_formal_import(
    session: Session,
    sources: Sequence[LoadedSource],
    *,
    known_domain_slugs: set[str] | None = None,
    domain_scope_policy: DomainScopePolicy | None = None,
) -> dict[str, Any]:
    """Import all selected sources in one database transaction.

    The caller controls the transaction boundary.  Raising from this function
    leaves the session ready for rollback, so no partial import becomes visible.
    """

    unregistered_domains = unregistered_source_domains(
        list(sources),
        known_domain_slugs=known_domain_slugs,
    )
    if unregistered_domains:
        raise ValueError(
            "Every source domain must be registered in config/taxonomy.yaml "
            f"before formal import: {unregistered_domains}"
        )

    known_domains = (
        load_domain_slugs() if known_domain_slugs is None else known_domain_slugs
    )
    scope_policy = domain_scope_policy or DomainScopePolicy.load(
        PROJECT_ROOT / "config" / "domain-scope-exclusions.yaml"
    )
    started = datetime.now(UTC)
    active_sources, retired_sources, historical_records_preserved = (
        _partition_sources_for_import(session, sources)
    )
    records = _load_all_records(active_sources)
    _validate_loaded_records(
        active_sources,
        records,
        known_domain_slugs=known_domains,
    )
    excluded_records = [
        record
        for record in records
        if set(record.asset.domain_slugs).intersection(
            scope_policy.excluded_domains(asset_id=None, title=record.asset.title)
        )
    ]
    excluded_record_keys = {
        f"{record.source_id}:{record.source_record_id}" for record in excluded_records
    }
    records = [
        record
        for record in records
        if f"{record.source_id}:{record.source_record_id}" not in excluded_record_keys
    ]
    rejected_keys = rejected_identity_keys(session)
    groups, reconciliation = identity_groups(records, rejected_keys=rejected_keys)
    source_models = _upsert_sources(session, active_sources)
    run = ImportRun(
        source_id=None,
        status=ImportStatus.RUNNING.value,
        mode="full",
        dry_run=False,
        adapter_name="multi-source",
        adapter_version=IMPORTER_VERSION,
        config_digest=_json_digest([source.raw for source in sources]),
        started_at=started,
    )
    session.add(run)
    session.flush()
    ctx = ImportContext(
        session=session,
        sources={source.source_id: source for source in sources},
        source_models=source_models,
        run=run,
        domain_scope_policy=scope_policy,
        rejected_keys=rejected_keys,
    )

    for group in groups:
        asset = _upsert_paper_group(ctx, group)
        for record in group:
            _, version = _upsert_source_record(ctx, record, asset)
            _write_claims(ctx, record, asset, version)

    # Materialize repositories before resolving any implemented_by edge.
    for record in records:
        paper = ctx.record_assets[f"{record.source_id}:{record.source_record_id}"]
        for representation_candidate in record.representations:
            _upsert_representation(ctx, record, representation_candidate, paper)

    for record in records:
        for index, relation_candidate in enumerate(record.relations):
            _upsert_relation(ctx, record, relation_candidate, index)

    _emit_dirty_asset_events(ctx)
    finished = datetime.now(UTC)
    counters = ctx.counters.as_dict()
    counters.update(
        {
            "paper_assets_total": len(
                {
                    asset.id
                    for asset in ctx.record_assets.values()
                    if asset.asset_type == "paper"
                }
            ),
            "repository_assets_total": len(ctx.repository_assets),
            "source_record_candidates": len(records),
            "representation_candidates": sum(len(item.representations) for item in records),
            "relation_candidates": sum(len(item.relations) for item in records),
            "strong_merge_groups": len(reconciliation["auto_merge_groups"]),
            "reviewed_merge_groups": len(reconciliation["approved_review_groups"]),
            "unapproved_merge_groups": len(
                reconciliation["unapproved_review_groups"]
            ),
        }
    )
    run.status = ImportStatus.SUCCEEDED.value
    run.finished_at = finished
    run.counters = counters
    session.add(
        Activity(
            activity_type="relation_reconciliation",
            tool_name="formal-importer",
            tool_version=IMPORTER_VERSION,
            status=ActivityStatus.SUCCEEDED.value,
            input_digest=run.config_digest,
            output_digest=_json_digest(counters),
            parameters={"import_run_id": str(run.id)},
            result={
                "counts": {
                    "resolved": ctx.counters.relation_candidates_resolved,
                    "unresolved": ctx.counters.relation_candidates_unresolved,
                },
                "unresolved_candidates": ctx.unresolved_relations,
            },
            started_at=started,
            finished_at=finished,
        )
    )
    session.flush()
    return {
        "schema_version": 2,
        "status": "passed",
        "import_run_id": str(run.id),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "source_roots_modified": False,
        "retirement_state_checked": True,
        "active_sources": [source.source_id for source in active_sources],
        "retired_sources_verified": retired_sources,
        "historical_records_preserved": historical_records_preserved,
        "scope_exclusions": {
            "source_records": len(excluded_records),
            "record_keys": sorted(excluded_record_keys),
        },
        "counters": counters,
        "unresolved_relation_candidates": ctx.unresolved_relations,
    }


def database_counts(session: Session) -> dict[str, int]:
    """Return release-gate counts from the current PostgreSQL or SQLite store."""

    def count(model: type[Any], *where: Any) -> int:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        return int(session.scalar(statement) or 0)

    return {
        "assets": count(Asset, Asset.deleted_at.is_(None)),
        "papers": count(
            Asset, Asset.asset_type == "paper", Asset.deleted_at.is_(None)
        ),
        "repositories": count(
            Asset, Asset.asset_type == "repository", Asset.deleted_at.is_(None)
        ),
        "source_records": count(SourceRecord),
        "representations": count(Representation, Representation.deleted_at.is_(None)),
        "missing_representations": count(
            Representation,
            Representation.availability == RepresentationAvailability.MISSING.value,
            Representation.deleted_at.is_(None),
        ),
        "relations": count(Relation, Relation.deleted_at.is_(None)),
        "implemented_by": count(
            Relation,
            Relation.relation_type == "implemented_by",
            Relation.deleted_at.is_(None),
        ),
        "field_claims": count(FieldClaim),
    }
