"""Shared, schema-agnostic helpers for the six legacy manifests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from packages.domain.enums import WarningSeverity
from packages.domain.schemas import AdapterWarning

from .base import BaseAdapter, int_or_none, string_or_none
from .types import AdapterError, AdapterErrorCode, ScannedRecord


def mapping_or_empty(value: Any) -> dict[str, Any]:
    """Return a string-keyed copy when a JSON value is an object."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def require_text(
    adapter: BaseAdapter,
    scanned: ScannedRecord,
    value: Any,
    *,
    field: str,
) -> str:
    text = string_or_none(value)
    if text is None:
        raise AdapterError(
            AdapterErrorCode.INVALID_RECORD,
            f"Manifest record {scanned.index} is missing required field '{field}'",
            path=adapter.manifest_absolute_path,
            details={"index": scanned.index, "field": field},
        )
    return text


def optional_year(
    adapter: BaseAdapter,
    value: Any,
    warnings: list[AdapterWarning],
    *,
    field: str = "year",
) -> int | None:
    if value is None or string_or_none(value) is None:
        warnings.append(
            adapter.warning(
                "missing_year",
                "No publication year was provided by the source",
                field=field,
            )
        )
        return None
    year = int_or_none(value)
    if year is None or not 1450 <= year <= 2200:
        warnings.append(
            adapter.warning(
                "invalid_year",
                f"Publication year is not valid: {value!r}",
                severity=WarningSeverity.ERROR,
                field=field,
                details={"value": value},
            )
        )
        return None
    return year


def warn_if_empty(
    adapter: BaseAdapter,
    warnings: list[AdapterWarning],
    value: Any,
    *,
    code: str,
    message: str,
    field: str,
) -> None:
    if value is None or value == "" or value == []:
        warnings.append(adapter.warning(code, message, field=field))


def mapping_index(
    values: Iterable[Any],
    key: str,
    *,
    stringify: bool = True,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        raw_key = value.get(key)
        if raw_key is None:
            continue
        normalized = str(raw_key) if stringify else raw_key
        result[normalized] = dict(value)
    return result


def grouped_mapping(
    values: Iterable[Any],
    key: str,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        if not isinstance(value, Mapping) or value.get(key) is None:
            continue
        result.setdefault(str(value[key]), []).append(dict(value))
    return result


def array_from_document(value: Any, *, keys: tuple[str, ...] = ("items", "papers")) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in keys:
            selected = value.get(key)
            if isinstance(selected, list):
                return selected
    return []


def strip_leading_parents(value: Any) -> str | None:
    text = string_or_none(value)
    if text is None:
        return None
    normalized = text.replace("\\", "/")
    while normalized.startswith("../"):
        normalized = normalized[3:]
    return normalized


def strip_prefix(value: Any, *prefixes: str) -> str | None:
    text = string_or_none(value)
    if text is None:
        return None
    normalized = text.replace("\\", "/")
    lowered = normalized.lower()
    for prefix in prefixes:
        candidate = prefix.replace("\\", "/").strip("/")
        with_slash = f"{candidate}/"
        if lowered.startswith(with_slash.lower()):
            return normalized[len(with_slash) :]
    return normalized


def repository_folder(url: str) -> str | None:
    normalized = url.rstrip("/")
    if "github.com/" not in normalized.lower():
        return None
    tail = normalized.split("github.com/", 1)[1]
    parts = [part for part in tail.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}__{parts[1].removesuffix('.git')}"
