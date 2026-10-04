from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.product_models import DomainDefinition


def list_domain_definitions(
    session: Session, settings: Any, *, include_hidden: bool = False
) -> list[dict[str, Any]]:
    """Legacy taxonomy remains immutable; SQL rows override only presentation."""
    path = Path(settings.taxonomy_config_path)
    document = (
        yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {"domains": []}
    )
    definitions = {
        str(item["id"]): {
            "slug": str(item["id"]),
            "label": str(item.get("label") or item["id"]),
            "aliases": list(item.get("aliases", [])),
            "description": item.get("description"),
            "tags": list(item.get("tags", [])),
            "hidden_from_catalog": bool(item.get("hidden_from_catalog")),
        }
        for item in document.get("domains", [])
    }
    for row in session.scalars(
        select(DomainDefinition).order_by(DomainDefinition.created_at, DomainDefinition.slug)
    ):
        hidden = definitions.get(row.slug, {}).get("hidden_from_catalog", False)
        definitions[row.slug] = {
            "slug": row.slug,
            "label": row.label,
            "aliases": row.aliases,
            "description": row.description,
            "tags": row.tags,
            "hidden_from_catalog": hidden,
        }
    return [
        {
            key: value
            for key, value in item.items()
            if include_hidden or key != "hidden_from_catalog"
        }
        for item in definitions.values()
        if include_hidden or not item["hidden_from_catalog"]
    ]


def domain_slugs(session: Session, taxonomy_path: Path) -> set[str]:
    from types import SimpleNamespace

    return {
        item["slug"]
        for item in list_domain_definitions(
            session, SimpleNamespace(taxonomy_config_path=taxonomy_path), include_hidden=True
        )
    }


def create_domain(session: Session, settings: Any, values: dict[str, Any]) -> DomainDefinition:
    label = values["label"].strip()
    known = {
        item["slug"] for item in list_domain_definitions(session, settings, include_hidden=True)
    }
    stem = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")[:48]
    if len(stem) < 2:
        stem = "domain-" + uuid4().hex[:16]
    slug = stem
    while slug in known:
        slug = stem + "-" + uuid4().hex[:8]
    row = DomainDefinition(slug=slug, **{**values, "label": label})
    session.add(row)
    session.flush()
    return row


def patch_domain(
    session: Session, settings: Any, slug: str, values: dict[str, Any]
) -> DomainDefinition | None:
    row = session.get(DomainDefinition, slug)
    if row is None:
        original = next(
            (
                item
                for item in list_domain_definitions(session, settings, include_hidden=True)
                if item["slug"] == slug
            ),
            None,
        )
        if original is None:
            return None
        row = DomainDefinition(
            **{key: value for key, value in original.items() if key != "hidden_from_catalog"}
        )
        session.add(row)
    for key, value in values.items():
        setattr(row, key, value.strip() if key == "label" else value)
    session.flush()
    return row
