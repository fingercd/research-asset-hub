"""Offline, source-located evidence extraction for paper HTML authoring."""

from .evidence import (
    CATEGORY_KEYS,
    TOOL_VERSION,
    EvidenceError,
    extract_record,
    load_paper_records,
    run_batch,
)

__all__ = [
    "CATEGORY_KEYS",
    "TOOL_VERSION",
    "EvidenceError",
    "extract_record",
    "load_paper_records",
    "run_batch",
]
