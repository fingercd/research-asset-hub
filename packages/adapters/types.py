"""Small adapter-runtime types that are intentionally not persisted.

The persisted interchange contract lives in :mod:`packages.domain.schemas`.
These dataclasses describe the read-only scan and preview stages around that
contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from packages.domain.schemas import NormalizedRecord


class AdapterErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_RECORD = "invalid_record"
    UNSUPPORTED_ROOT = "unsupported_root"
    MISSING_MANIFEST = "missing_manifest"
    MISSING_AUXILIARY = "missing_auxiliary"
    PATH_OUTSIDE_ROOT = "path_outside_root"
    UNKNOWN_ROOT_KEY = "unknown_root_key"
    DUPLICATE_SOURCE_RECORD_ID = "duplicate_source_record_id"
    MISSING_SOURCE_RECORD_ID = "missing_source_record_id"
    PLUGIN_LOAD_FAILED = "plugin_load_failed"
    UNKNOWN_ADAPTER = "unknown_adapter"


class AdapterError(RuntimeError):
    """A structured adapter failure suitable for a dry-run report."""

    def __init__(
        self,
        code: AdapterErrorCode | str,
        message: str,
        *,
        path: Path | str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = AdapterErrorCode(code)
        self.path = str(path) if path is not None else None
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "path": self.path,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class ScannedRecord:
    """One JSON object found at a stable position in a declared manifest."""

    index: int
    raw: dict[str, Any]
    source_path: str


@dataclass(frozen=True, slots=True)
class AdapterPreview:
    """Compact, serializable summary returned by ``BaseAdapter.preview``."""

    source_id: str
    adapter_name: str
    total_records: int
    outcomes: dict[str, int]
    warning_counts: dict[str, int]
    representation_counts: dict[str, int]
    relation_counts: dict[str, int]
    sample_records: tuple[NormalizedRecord, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "adapter_name": self.adapter_name,
            "total_records": self.total_records,
            "outcomes": dict(self.outcomes),
            "warning_counts": dict(self.warning_counts),
            "representation_counts": dict(self.representation_counts),
            "relation_counts": dict(self.relation_counts),
            "sample_records": [record.model_dump(mode="json") for record in self.sample_records],
        }
