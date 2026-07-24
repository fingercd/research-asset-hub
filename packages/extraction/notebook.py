"""Jupyter notebook extraction without executing notebook code."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import (
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    NotebookAnchor,
    hash_bytes,
    make_chunk,
)


def _joined_source(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return "".join(str(item) for item in value)
    return ""


class NotebookExtractor:
    name = "notebook"
    version = "1"

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() == ".ipynb"

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        raw = path.read_bytes()
        representation_hash = hash_bytes(raw)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=representation_hash,
                extractor=self.name,
                extractor_version=self.version,
                status=ExtractionStatus.FAILED,
                warnings=(f"Invalid notebook JSON: {exc}",),
            )
        cells = document.get("cells", []) if isinstance(document, Mapping) else []
        chunks: list[ExtractionChunk] = []
        warnings: list[str] = []
        for cell_index, cell in enumerate(cells):
            if not isinstance(cell, Mapping):
                continue
            cell_type = str(cell.get("cell_type", "unknown"))
            source = _joined_source(cell.get("source")).strip()
            if source:
                anchor: NotebookAnchor = {
                    "kind": "notebook",
                    "cell_index": cell_index,
                    "cell_type": cell_type,
                }
                chunks.append(
                    make_chunk(
                        representation_id=representation_id,
                        representation_hash=representation_hash,
                        ordinal=len(chunks),
                        text=source,
                        extractor=self.name,
                        extractor_version=self.version,
                        anchor=anchor,
                        title=f"{cell_type} cell {cell_index}",
                    )
                )
            if cell_type != "code":
                continue
            outputs = cell.get("outputs", [])
            if not isinstance(outputs, Sequence):
                continue
            for output_index, output in enumerate(outputs):
                if not isinstance(output, Mapping):
                    continue
                output_text = _joined_source(output.get("text")).strip()
                data = output.get("data")
                if not output_text and isinstance(data, Mapping):
                    output_text = _joined_source(data.get("text/plain")).strip()
                    if any(
                        str(key).startswith("image/")
                        for key in data
                    ):
                        warnings.append(
                            f"Skipped binary image output in cell {cell_index}"
                        )
                if not output_text:
                    continue
                output_anchor: NotebookAnchor = {
                    "kind": "notebook",
                    "cell_index": cell_index,
                    "cell_type": "output",
                    "output_index": output_index,
                }
                chunks.append(
                    make_chunk(
                        representation_id=representation_id,
                        representation_hash=representation_hash,
                        ordinal=len(chunks),
                        text=output_text,
                        extractor=self.name,
                        extractor_version=self.version,
                        anchor=output_anchor,
                        title=f"Output {cell_index}:{output_index}",
                    )
                )
        return ExtractionResult(
            representation_id=representation_id,
            source_path=str(path),
            representation_hash=representation_hash,
            extractor=self.name,
            extractor_version=self.version,
            status=(
                ExtractionStatus.SUCCEEDED
                if chunks and not warnings
                else ExtractionStatus.PARTIAL
                if chunks
                else ExtractionStatus.FAILED
            ),
            chunks=tuple(chunks),
            warnings=tuple(warnings),
            metadata={"cell_count": len(cells)},
        )
