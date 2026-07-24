"""Lossless newline-delimited JSON export."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..schemas import NormalizedRecord
from .common import atomic_write_text, jsonable


def _envelope(record_type: str, payload: Any) -> dict[str, Any]:
    return {
        "export_schema": "research-asset-hub.catalog/v1",
        "record_type": record_type,
        "payload": jsonable(payload),
    }


def export_jsonl(
    records: Iterable[NormalizedRecord],
    destination: str | Path,
    *,
    reading_states: Iterable[dict[str, Any]] = (),
    annotations: Iterable[dict[str, Any]] = (),
    relations: Iterable[dict[str, Any]] = (),
) -> Path:
    """Export catalog state so it can be read without a database.

    The optional streams carry platform-authored state.  Adapter relations are
    already embedded in each ``NormalizedRecord``; ``relations`` is for edges
    created or edited in the platform after import.
    """

    lines: list[str] = []
    streams = (
        ("normalized_record", records),
        ("reading_state", reading_states),
        ("annotation", annotations),
        ("relation", relations),
    )
    for record_type, values in streams:
        for value in values:
            lines.append(
                json.dumps(
                    _envelope(record_type, value),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    text = "\n".join(lines)
    if lines:
        text += "\n"
    return atomic_write_text(Path(destination), text)
