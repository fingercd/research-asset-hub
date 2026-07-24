"""Canonical search filters and backend-specific translations."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard

_ALIASES = {
    "domain": "domains",
    "type": "asset_type",
    "status": "reading_status",
    "pdf": "has_pdf",
    "html": "has_html",
    "code": "has_code",
    "guide": "has_guide",
}
_SUPPORTED = frozenset(
    {
        "domains",
        "year",
        "year_from",
        "year_to",
        "asset_type",
        "reading_status",
        "tags",
        "has_pdf",
        "has_html",
        "has_code",
        "has_guide",
        "representation_types",
        "available_representation_types",
        "availability",
    }
)
_BOOLEAN_FILTERS = frozenset({"has_pdf", "has_html", "has_code", "has_guide"})


def canonical_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize public filter aliases and reject keys the indexes cannot honor."""

    normalized: dict[str, Any] = {}
    for raw_key, value in (filters or {}).items():
        key = _ALIASES.get(raw_key, raw_key)
        if key not in _SUPPORTED or value is None or value == "":
            continue
        if key in _BOOLEAN_FILTERS and isinstance(value, str):
            lowered = value.strip().casefold()
            if lowered in {"true", "1", "yes"}:
                value = True
            elif lowered in {"false", "0", "no"}:
                value = False
        normalized[key] = value
    return normalized


def metadata_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the subset directly comparable with ``SearchDocument.metadata``."""

    return {
        key: value
        for key, value in canonical_filters(filters).items()
        if key not in {"year_from", "year_to"}
    }


def matches_metadata(metadata: Mapping[str, Any], filters: Mapping[str, Any] | None) -> bool:
    normalized = canonical_filters(filters)
    year = metadata.get("year")
    lower = normalized.pop("year_from", None)
    upper = normalized.pop("year_to", None)
    if lower is not None and (not isinstance(year, int) or year < int(lower)):
        return False
    if upper is not None and (not isinstance(year, int) or year > int(upper)):
        return False
    for key, expected in normalized.items():
        actual = metadata.get(key)
        if actual is None and key == "domains":
            actual = metadata.get("domain")
        elif actual is None and key == "asset_type":
            actual = metadata.get("kind")
        elif actual is None and key == "reading_status":
            actual = metadata.get("status")
        if _is_sequence(expected):
            expected_values = set(expected)
            if _is_sequence(actual):
                if not expected_values.intersection(actual):
                    return False
            elif actual not in expected_values:
                return False
        elif _is_sequence(actual):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


def meilisearch_filter(filters: Mapping[str, Any] | None) -> list[str]:
    """Translate canonical filters to Meilisearch filter expressions."""

    normalized = canonical_filters(filters)
    clauses: list[str] = []
    lower = normalized.pop("year_from", None)
    upper = normalized.pop("year_to", None)
    if lower is not None:
        clauses.append(f"metadata.year >= {int(lower)}")
    if upper is not None:
        clauses.append(f"metadata.year <= {int(upper)}")
    for key, value in normalized.items():
        field = f"metadata.{key}"
        if _is_sequence(value):
            rendered = ", ".join(_meili_literal(item) for item in value)
            clauses.append(f"{field} IN [{rendered}]")
        else:
            clauses.append(f"{field} = {_meili_literal(value)}")
    return clauses


def qdrant_filter(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Translate canonical filters to Qdrant's payload filter format."""

    normalized = canonical_filters(filters)
    must: list[dict[str, Any]] = []
    lower = normalized.pop("year_from", None)
    upper = normalized.pop("year_to", None)
    if lower is not None or upper is not None:
        range_value: dict[str, int] = {}
        if lower is not None:
            range_value["gte"] = int(lower)
        if upper is not None:
            range_value["lte"] = int(upper)
        must.append({"key": "metadata.year", "range": range_value})
    for key, value in normalized.items():
        match: dict[str, Any]
        if _is_sequence(value):
            match = {"any": list(value)}
        else:
            match = {"value": value}
        must.append({"key": f"metadata.{key}", "match": match})
    return {"must": must} if must else {}


def _is_sequence(value: object) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _meili_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)
