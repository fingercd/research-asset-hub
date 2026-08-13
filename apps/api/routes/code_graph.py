"""领域路由：代码图谱。"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import select

from apps.api import routes_v1 as _v1
from apps.api.editor import CODE_REPRESENTATION_TYPES, resolve_editor_path
from apps.api.routes_v1 import (
    _gitnexus_registry_index,
    _load_code_graph_hidden,
    _load_yaml,
    router,
)
from packages.domain.models import Asset, Representation, Source


@router.get("/code-graph")
def code_graph(request: Request) -> dict[str, Any]:
    """Read-only repository inventory grouped by research direction."""

    settings = _v1._settings(request)
    source_registry = _v1._source_registry(request)
    gitnexus_registry = _gitnexus_registry_index()
    gitnexus_paths = set(gitnexus_registry)
    hidden_rules = _load_code_graph_hidden(settings)
    hidden_registry_names = hidden_rules["registry_names"]
    hidden_title_patterns = hidden_rules["title_patterns"]
    taxonomy = _load_yaml(settings.taxonomy_config_path)
    domain_labels = {
        str(item.get("id")): str(item.get("label") or item.get("id"))
        for item in taxonomy.get("domains", [])
        if isinstance(item, dict)
    }

    with _v1._database(request).session() as session:
        rows = session.execute(
            select(Representation, Asset, Source)
            .join(Asset, Asset.id == Representation.asset_id)
            .join(Source, Source.id == Representation.source_id)
            .where(
                Representation.deleted_at.is_(None),
                Asset.deleted_at.is_(None),
                Representation.representation_type.in_(CODE_REPRESENTATION_TYPES),
            )
            .order_by(Asset.title, Representation.id)
        ).all()

    assets: dict[UUID, dict[str, Any]] = {}
    for representation, asset, source in rows:
        resolution = resolve_editor_path(
            source_registry,
            representation,
            source_alias=str(source.alias),
        )
        entry = assets.setdefault(
            asset.id,
            {
                "id": str(asset.id),
                "title": asset.title,
                "title_zh": asset.title_zh,
                "asset_type": asset.asset_type,
                "year": asset.year,
                "domain_slugs": list(asset.domain_slugs or []),
                "paths": [],
                "representation_count": 0,
            },
        )
        entry["representation_count"] += 1
        if resolution.available and resolution.path:
            normalized = str(Path(resolution.path).resolve())
            if normalized not in entry["paths"]:
                entry["paths"].append(normalized)

    for entry in assets.values():
        indexed_path = next(
            (
                path
                for path in entry["paths"]
                if path.casefold() in gitnexus_paths
            ),
            None,
        )
        entry["gitnexus_indexed"] = indexed_path is not None
        entry["gitnexus_repo_name"] = (
            (
                gitnexus_registry[indexed_path.casefold()].get("name")
                or None
            )
            if indexed_path is not None
            else None
        )
        title = (entry["title"] or entry["title_zh"] or "").casefold()
        repo_name = (entry["gitnexus_repo_name"] or "").casefold()
        entry["hidden"] = (
            repo_name in hidden_registry_names
            or any(pattern in title for pattern in hidden_title_patterns)
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_hidden: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in assets.values():
        slugs = [slug for slug in entry["domain_slugs"] if slug]
        if slugs:
            for slug in slugs:
                (grouped_hidden if entry["hidden"] else grouped)[slug].append(
                    entry
                )
        else:
            (grouped_hidden if entry["hidden"] else grouped)[
                "unassigned"
            ].append(entry)
    for slug in grouped:
        grouped[slug].sort(
            key=lambda item: (item["title_zh"] or item["title"]).casefold()
        )
    for slug in grouped_hidden:
        grouped_hidden[slug].sort(
            key=lambda item: (item["title_zh"] or item["title"]).casefold()
        )

    domains = [
        {
            "slug": slug,
            "label": domain_labels.get(
                slug,
                "未归类" if slug == "unassigned" else slug,
            ),
            "repository_count": len(grouped.get(slug, [])),
            "indexed_count": sum(
                1
                for item in grouped.get(slug, [])
                if item["gitnexus_indexed"]
            ),
            "repositories": grouped.get(slug, []),
            "hidden_repositories": grouped_hidden.get(slug, []),
            "hidden_count": len(grouped_hidden.get(slug, [])),
        }
        for slug, _ in sorted(
            (grouped | grouped_hidden).items(),
            key=lambda item: item[0],
        )
    ]
    repositories = [
        entry for entry in assets.values() if not entry["hidden"]
    ]
    hidden_count = sum(1 for entry in assets.values() if entry["hidden"])
    return {
        "total_count": len(repositories),
        "indexed_count": sum(1 for item in repositories if item["gitnexus_indexed"]),
        "hidden_count": hidden_count,
        "domains": domains,
    }
