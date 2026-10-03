"""Cheap, read-only library coverage from SQL ledgers and root registration.

This endpoint does not verify source files, extraction digests, or a remote
search engine. It deliberately keeps those claims separate from ledger counts.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, or_, select, tuple_, union_all
from sqlalchemy.orm import Session, load_only

from apps.api.domain_views import annotate_presentation_domains, presentation_domains
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import (
    Activity,
    Annotation,
    Asset,
    DocumentChunk,
    IndexState,
    Job,
    Representation,
    Source,
)
from packages.domain.serving_scope import active_representation_clause


def _catalog(settings: Any) -> list[dict[str, Any]]:
    document = yaml.safe_load(Path(settings.taxonomy_config_path).read_text(encoding="utf-8"))
    return [
        {
            "slug": item["id"],
            "label": item.get("label") or item["id"],
            "aliases": item.get("aliases", []),
        }
        for item in document.get("domains", [])
        if not item.get("hidden_from_catalog")
    ]


def build_knowledge_status(
    session: Session,
    registry: SourceRegistry,
    cache_root: Path,
    settings: Any,
    domain: str | None = None,
) -> dict[str, Any]:
    """Return bounded aggregate queries; never read chunk bodies or scan assets.

    ``domain`` is a normalized presentation-domain slug. All metadata assets are
    loaded with only the fields needed by the existing domain presentation rule;
    the set includes source papers so repository classification needs no N+1.
    """
    with session.no_autoflush:
        return _build_status(session, registry, cache_root, settings, domain)


def _build_status(
    session: Session, registry: SourceRegistry, cache_root: Path, settings: Any, domain: str | None
) -> dict[str, Any]:
    configuration = registry.configuration_status()
    root_status = registry.root_status()
    roots = registry.describe()
    roots_by_alias: dict[str, set[str]] = {}
    for root in roots:
        roots_by_alias.setdefault(root.source_id, set()).add(root.root_key)
    configuration["matches_settings_configuration_path"] = (
        registry.config_path == Path(settings.sources_config_path).expanduser().resolve()
        if registry.config_path is not None
        else None
    )
    assets = list(
        session.scalars(
            select(Asset).options(
                load_only(
                    Asset.id,
                    Asset.asset_type,
                    Asset.title,
                    Asset.domain_slugs,
                    Asset.tags,
                    Asset.attributes,
                    Asset.deleted_at,
                )
            )
        )
    )
    annotate_presentation_domains(session, assets)
    assets = [
        asset
        for asset in assets
        if asset.deleted_at is None
        and str((asset.attributes or {}).get("system_hidden", "")).casefold() != "true"
    ]
    domains = _catalog(settings)
    known = {item["slug"] for item in domains}
    # New taxonomy entries and previously uncatalogued presentation slugs remain visible.
    discovered = {slug for asset in assets for slug in presentation_domains(asset)}
    for slug in sorted(discovered - known):
        domains.append({"slug": slug, "label": slug, "aliases": []})
    for item in domains:
        matching = [asset for asset in assets if item["slug"] in presentation_domains(asset)]
        item.update(
            asset_count=len(matching),
            paper_count=sum(asset.asset_type == "paper" for asset in matching),
            repository_count=sum(asset.asset_type == "repository" for asset in matching),
        )
    selected = [
        asset for asset in assets if domain is None or domain in presentation_domains(asset)
    ]
    asset_ids = {asset.id for asset in selected}
    # Group the representation ledger, including disabled/retired sources, so
    # those unavailable records do not vanish from status totals.
    rep_scope = select(Representation.id).where(
        Representation.asset_id.in_(asset_ids), Representation.deleted_at.is_(None)
    )
    active_rep_scope = rep_scope.join(Source).where(
        Source.enabled.is_(True), active_representation_clause()
    )
    active_rep_assets = {
        representation_id: asset_id
        for representation_id, asset_id in session.execute(
            select(Representation.id, Representation.asset_id).where(
                Representation.id.in_(active_rep_scope)
            )
        ).all()
    }
    # Resolve serving per representation before aggregation. PostgreSQL rejects
    # correlated serving predicates in GROUP BY because they read ungrouped IDs.
    ledger = (
        select(
            Source.alias.label("alias"),
            Source.enabled.label("enabled"),
            Representation.root_key.label("root_key"),
            Representation.representation_type.label("representation_type"),
            Representation.availability.label("availability"),
            Representation.path_ref.is_not(None).label("local"),
            active_representation_clause().label("active"),
            Representation.id.label("id"),
            Representation.asset_id.label("asset_id"),
        )
        .outerjoin(Source, Source.id == Representation.source_id)
        .where(Representation.id.in_(rep_scope))
        .subquery()
    )
    ledger_columns = (
        ledger.c.alias,
        ledger.c.enabled,
        ledger.c.root_key,
        ledger.c.representation_type,
        ledger.c.availability,
        ledger.c.local,
        ledger.c.active,
    )
    rep_rows = session.execute(
        select(
            *ledger_columns,
            func.count(ledger.c.id),
            func.count(func.distinct(ledger.c.asset_id)),
        )
        .group_by(*ledger_columns)
    ).all()
    rep_types: dict[str, dict[str, Any]] = {}
    source_rep_counts: Counter[str] = Counter()
    unavailable: Counter[str] = Counter()
    ledger_available = 0
    registered_local = 0
    for alias, enabled, root_key, rep_type, availability, local, active, count, _ in rep_rows:
        bucket = rep_types.setdefault(
            rep_type, {"representation_type": rep_type, "count": 0, "availability_counts": {}}
        )
        bucket["count"] += count
        bucket["availability_counts"][availability] = (
            bucket["availability_counts"].get(availability, 0) + count
        )
        if alias:
            source_rep_counts[alias] += count
        reason = None
        if not active:
            reason = "source_retired"
        elif not enabled:
            reason = "source_disabled_or_absent"
        elif availability != "available":
            reason = f"representation_{availability}"
        elif not local:
            reason = "remote_location_not_verified"
        elif root_key not in roots_by_alias.get(alias, set()):
            reason = "root_not_registered"
        else:
            registered_local += count
        if enabled and active and availability == "available":
            ledger_available += count
        if reason:
            unavailable[reason] += count
    # Count representations with chunks, never select text_content or anchor.
    chunk_counts = session.execute(
        select(
            DocumentChunk.representation_id,
            func.count(),
        )
        .where(DocumentChunk.representation_id.in_(active_rep_assets))
        .group_by(DocumentChunk.representation_id)
    ).all()
    # Every chunk belongs to its representation's asset. Only the indexed
    # representation_id and count(*) are needed, avoiding the large text heap.
    chunk_summary = (
        sum(count for _, count in chunk_counts),
        len(chunk_counts),
        len({active_rep_assets[representation_id] for representation_id, _ in chunk_counts}),
    )
    latest_extraction = (
        select(
            Activity.representation_id.label("representation_id"),
            Activity.status.label("status"),
            Activity.result["status"].as_string().label("result_status"),
            func.row_number()
            .over(
                partition_by=Activity.representation_id,
                order_by=(Activity.started_at.desc(), Activity.id.desc()),
            )
            .label("position"),
        )
        .where(
            Activity.activity_type == "extraction",
            Activity.representation_id.in_(active_rep_assets),
        )
        .subquery()
    )
    extraction_counts = [
        {"activity_status": status, "result_status": result_status, "representations": count}
        for status, result_status, count in session.execute(
            select(latest_extraction.c.status, latest_extraction.c.result_status, func.count())
            .where(latest_extraction.c.position == 1)
            .group_by(latest_extraction.c.status, latest_extraction.c.result_status)
            .order_by(latest_extraction.c.status, latest_extraction.c.result_status)
        )
    ]
    active_representations = len(active_rep_assets)
    job_statement = select(Job.job_type, Job.status, func.count(Job.id)).where(
        Job.status.in_(["pending", "running", "failed"])
    )
    if domain is not None:
        representation_ids = [str(value) for value in session.scalars(rep_scope)]
        job_statement = job_statement.where(
            or_(
                Job.parameters["asset_id"].as_string().in_([str(value) for value in asset_ids]),
                Job.parameters["representation_id"].as_string().in_(representation_ids),
            )
        )
    job_rows = session.execute(
        job_statement.group_by(Job.job_type, Job.status).order_by(Job.job_type, Job.status)
    ).all()
    jobs_by_type: dict[str, dict[str, int]] = {}
    jobs_totals = {"pending": 0, "running": 0, "failed": 0}
    for job_type, status, count in job_rows:
        jobs_by_type.setdefault(job_type, {"pending": 0, "running": 0, "failed": 0})[status] = count
        jobs_totals[status] += count
    sources = []
    database_aliases: set[str] = set()
    for source in session.scalars(
        select(Source)
        .options(load_only(Source.alias, Source.label, Source.enabled, Source.config))
        .order_by(Source.alias)
    ):
        database_aliases.add(source.alias)
        declared_domains = source.config.get(
            "domain_slugs", source.config.get("domains", [source.config.get("domain")])
        )
        if (
            domain is not None
            and not source_rep_counts[source.alias]
            and domain not in (declared_domains or [])
        ):
            continue
        sources.append(
            {
                "alias": source.alias,
                "label": source.label,
                "enabled": source.enabled,
                "registered": source.alias in roots_by_alias,
                "configured_root_count": len(roots_by_alias.get(source.alias, set())),
                "representation_count": source_rep_counts[source.alias],
                "verification": "ledger_and_root_registration_only",
            }
        )
    # The default status describes the whole index ledger, including historical
    # rows. A scoped chunk membership join and DISTINCT over hundreds of
    # thousands of chunk entities belongs only to explicit domain requests.
    global_index_rows = session.execute(
        select(
            IndexState.version,
            IndexState.index_name,
            IndexState.entity_type,
            IndexState.status,
            func.count(),
        )
        .where(IndexState.index_kind.in_(["keyword", "code"]))
        .group_by(
            IndexState.version, IndexState.index_name, IndexState.entity_type, IndexState.status
        )
    ).all()
    names_by_entity: dict[str, set[str]] = {}
    for _, name, entity_type, _, _ in global_index_rows:
        names_by_entity.setdefault(entity_type, set()).add(name)
    aggregated_index_states = (
        select(
            IndexState.version,
            IndexState.index_name,
            IndexState.entity_type,
            IndexState.status,
            func.count(IndexState.id),
            func.count(func.distinct(IndexState.entity_id)),
        )
        .where(IndexState.index_kind.in_(["keyword", "code"]))
        .group_by(
            IndexState.version, IndexState.index_name, IndexState.entity_type, IndexState.status
        )
    )
    index_branches = [
        aggregated_index_states.where(
            IndexState.entity_type == "asset",
            IndexState.entity_id.in_(asset_ids),
            IndexState.index_name.in_(names_by_entity.get("asset", set())),
        ),
        aggregated_index_states.join(DocumentChunk, DocumentChunk.id == IndexState.entity_id).where(
            IndexState.entity_type == "chunk",
            DocumentChunk.representation_id.in_(active_rep_assets),
            IndexState.index_name.in_(names_by_entity.get("chunk", set())),
        ),
        aggregated_index_states.join(Annotation, Annotation.id == IndexState.entity_id).where(
            IndexState.entity_type == "annotation",
            Annotation.asset_id.in_(asset_ids),
            Annotation.deleted_at.is_(None),
            IndexState.index_name.in_(names_by_entity.get("annotation", set())),
        ),
    ]
    if domain is None:
        # Existing (index_name, entity_type, entity_id, ...) uniqueness indexes
        # cover these small non-chunk partitions. Keep exact distinct counts
        # here without sorting the full chunk ledger.
        small_partitions = {
            (name, entity_type)
            for _, name, entity_type, _, _ in global_index_rows
            if entity_type in {"asset", "annotation"}
        }
        distinct_counts = (
            {
                (version, name, entity_type, status): count
                for version, name, entity_type, status, count in session.execute(
                    select(
                        IndexState.version,
                        IndexState.index_name,
                        IndexState.entity_type,
                        IndexState.status,
                        func.count(func.distinct(IndexState.entity_id)),
                    )
                    .where(
                        IndexState.index_kind.in_(["keyword", "code"]),
                        tuple_(IndexState.index_name, IndexState.entity_type).in_(small_partitions),
                    )
                    .group_by(
                        IndexState.version,
                        IndexState.index_name,
                        IndexState.entity_type,
                        IndexState.status,
                    )
                )
            }
            if small_partitions
            else {}
        )
        index_rows = [
            (
                version,
                name,
                entity_type,
                status,
                count,
                distinct_counts.get((version, name, entity_type, status)),
            )
            for version, name, entity_type, status, count in global_index_rows
        ]
        index_rows.sort(key=lambda item: item[:4])
    else:
        index_rows = [
            (version, name, entity_type, status, row_count, entity_count)
            for version, name, entity_type, status, row_count, entity_count in session.execute(
                union_all(*index_branches).order_by(
                    "version", "index_name", "entity_type", "status"
                )
            ).all()
        ]
    index_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for version, name, entity_type, status, count, entities in index_rows:
        item = index_groups.setdefault(
            (version, name, entity_type),
            {
                "version": version,
                "version_role": "formal"
                if version == "v1"
                else "legacy"
                if version == "1"
                else "other",
                "index_name": name,
                "entity_type": entity_type,
                "serving_enabled": name != "documents"
                and (name != "code" or settings.search_code_index_enabled),
                "state_counts": {},
                "distinct_entities_by_state": {},
                "verification": "ledger_only",
                "distinct_entities_computed": entities is not None,
            },
        )
        item["state_counts"][status] = count
        item["distinct_entities_by_state"][status] = entities
    return {
        "read_only": True,
        "schema_version": 2,
        "domain": domain,
        "backend": "postgresql_metadata",
        "search_backend": "meilisearch",
        "search_fallback": "postgresql_metadata",
        "cache_available": cache_root.is_dir(),
        "document_chunks": int(chunk_summary[0]),
        "limits": {"search_results": 20, "evidence_chars": 16000},
        "ocr_on_read": False,
        "configuration": configuration,
        "domains": domains,
        "counts": {
            "assets": len(selected),
            "papers": sum(asset.asset_type == "paper" for asset in selected),
            "repositories": sum(asset.asset_type == "repository" for asset in selected),
            "representations": sum(row[7] for row in rep_rows),
        },
        "sources": sources,
        "configured_sources_not_in_database": sorted(roots_by_alias.keys() - database_aliases),
        "source_roots": {
            key: root_status[key] for key in ("configured", "verification", "source_files_verified")
        },
        "representations": {
            "by_type": list(rep_types.values()),
            "active_count": active_representations,
            "ledger_available_count": ledger_available,
            "representations_with_registered_root": registered_local,
            "unavailable_or_unverified_reasons": dict(sorted(unavailable.items())),
            "verification": "ledger_and_root_registration_only",
            "source_files_verified": False,
        },
        "extraction": {
            "latest_activity_counts": extraction_counts,
            "without_activity_count": active_representations
            - sum(item["representations"] for item in extraction_counts),
            "representations_with_chunks": chunk_summary[1],
            "assets_with_chunks": chunk_summary[2],
            "verification": "ledger_only",
            "source_digests_verified": False,
        },
        "jobs": {
            "counts": jobs_totals,
            "by_type": jobs_by_type,
            "scope": "library" if domain is None else "domain_assets_and_representations",
        },
        "keyword_index": {
            "formal_version": "v1",
            "legacy_version": "1",
            "states": list(index_groups.values()),
            "verification": "ledger_only",
            "remote_service_verified": False,
            "current_content_digests_verified": False,
            "index_ledger_scope": "all_index_ledger_entities"
            if domain is None
            else "active_domain_entities",
            "active_scope_applied": domain is not None,
            "may_include_hidden_or_retired_entities": domain is None,
            "distinct_count_note": (
                "全库chunk实体去重数量未计算，以null表示；台账包含历史和不可见实体。"
                if domain is None
                else "按当前领域可见资产和有效表示范围计算实体去重数量。"
            ),
            "coverage_note": (
                "索引台账行数与不同实体计数不等于当前正文覆盖；远端服务和内容摘要未实测。"
            ),
        },
        "verification_limits": [
            "未扫描PDF或代码目录",
            "未验证源文件摘要与抽取缓存",
            "未连接远端关键词引擎",
            "未触发任务或建立向量索引",
        ],
    }
