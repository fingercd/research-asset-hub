"""Streaming-oriented CSV, JSON and JSONL extraction."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import read_text
from .models import (
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    TabularAnchor,
    hash_bytes,
    make_chunk,
)


class TabularExtractor:
    name = "tabular"
    version = "1"

    def __init__(self, *, full_index_bytes: int = 2_097_152, sample_rows: int = 100) -> None:
        self.full_index_bytes = full_index_bytes
        self.sample_rows = sample_rows

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() in {".csv", ".json", ".jsonl", ".ndjson"}

    def _rows(self, path: Path, text: str) -> tuple[list[Any], list[str]]:
        suffix = path.suffix.casefold()
        if suffix == ".csv":
            reader = csv.DictReader(text.splitlines())
            rows = [dict(row) for row in reader]
            return rows, list(reader.fieldnames or [])
        if suffix in {".jsonl", ".ndjson"}:
            rows = [
                json.loads(line)
                for line in text.splitlines()
                if line.strip()
            ]
            columns = sorted(
                {
                    str(key)
                    for row in rows
                    if isinstance(row, Mapping)
                    for key in row
                }
            )
            return rows, columns
        document = json.loads(text)
        rows = (
            list(document)
            if isinstance(document, Sequence) and not isinstance(document, str)
            else [document]
        )
        columns = sorted(
            {
                str(key)
                for row in rows
                if isinstance(row, Mapping)
                for key in row
            }
        )
        return rows, columns

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        raw = path.read_bytes()
        representation_hash = hash_bytes(raw)
        text, encoding = read_text(path)
        try:
            rows, columns = self._rows(path, text)
        except (csv.Error, json.JSONDecodeError) as exc:
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=representation_hash,
                extractor=self.name,
                extractor_version=self.version,
                status=ExtractionStatus.FAILED,
                warnings=(f"Tabular parse failed: {exc}",),
                metadata={"encoding": encoding},
            )

        sampled = len(raw) > self.full_index_bytes
        selected_rows = rows[: self.sample_rows] if sampled else rows
        text_rows = [
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            for row in selected_rows
        ]
        chunks: list[ExtractionChunk] = []
        block_size = 100
        for offset in range(0, len(text_rows), block_size):
            block = text_rows[offset : offset + block_size]
            anchor: TabularAnchor = {
                "kind": "tabular",
                "row_start": offset + 1,
                "row_end": offset + len(block),
                "columns": columns,
            }
            chunks.append(
                make_chunk(
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                    ordinal=len(chunks),
                    text="\n".join(block),
                    extractor=self.name,
                    extractor_version=self.version,
                    anchor=anchor,
                    title=path.name,
                    metadata={"sampled": sampled},
                )
            )
        warnings = (
            (f"Indexed a {len(selected_rows)} row sample of {len(rows)} rows",)
            if sampled
            else ()
        )
        return ExtractionResult(
            representation_id=representation_id,
            source_path=str(path),
            representation_hash=representation_hash,
            extractor=self.name,
            extractor_version=self.version,
            status=ExtractionStatus.PARTIAL if sampled else ExtractionStatus.SUCCEEDED,
            chunks=tuple(chunks),
            warnings=warnings,
            metadata={
                "encoding": encoding,
                "row_count": len(rows),
                "columns": columns,
                "sampled": sampled,
            },
        )
