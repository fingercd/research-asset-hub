"""Resolve user-facing domain names without rewriting frozen source classifications."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from apps.api.domain_views import presentation_domains

_HISTORICAL = {"generation", "navigation-compression"}
_SOURCE_CANDIDATES = {
    "image-editing": ("image-editing", "generation"),
    "video-generation": ("video-generation", "generation"),
    "embodied-ai-compression": ("embodied-ai-compression", "navigation-compression"),
}


def normalize_domain(value: str | None, taxonomy_path: Path | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if taxonomy_path is None:
        return value
    data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8"))
    rows = data["domains"]
    # A stable identifier wins over an alias shared by more than one domain.
    for item in rows:
        if value.casefold() == item["id"].casefold():
            return str(item["id"])
    matches = {
        str(item["id"])
        for item in rows
        if not item.get("hidden_from_catalog")
        and value.casefold()
        in {str(name).casefold() for name in [item.get("label", ""), *item.get("aliases", [])]}
    }
    if len(matches) == 1:
        return next(iter(matches))
    if not matches:
        raise ValueError("Unknown domain. Use library_status to discover registered domains.")
    raise ValueError("Ambiguous domain alias. Use the stable domain slug from library_status.")


def source_domain_candidates(domain: str | None) -> tuple[str, ...]:
    if domain is None:
        return ()
    return _SOURCE_CANDIDATES.get(domain, (domain,))


def matches_domain(asset: Any, domain: str | None) -> bool:
    if domain is None:
        return True
    if domain in _HISTORICAL:
        return domain in asset.domain_slugs
    return domain in presentation_domains(asset)
