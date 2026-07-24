"""Markdown and plain-text extraction with heading and line anchors."""

from __future__ import annotations

import re
from pathlib import Path

from .common import read_text
from .models import (
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    LineAnchor,
    hash_bytes,
    make_chunk,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class MarkdownExtractor:
    name = "markdown"
    version = "1"

    def __init__(self, *, max_lines: int = 160) -> None:
        self.max_lines = max_lines

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() in {
            ".md",
            ".markdown",
            ".txt",
            ".rst",
            ".log",
        }

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        raw = path.read_bytes()
        representation_hash = hash_bytes(raw)
        text, encoding = read_text(path)
        lines = text.splitlines()
        chunks: list[ExtractionChunk] = []
        heading = path.name
        start = 1
        buffer: list[str] = []

        def flush(end_line: int) -> None:
            nonlocal start, buffer
            content = "\n".join(buffer).strip()
            if content:
                anchor: LineAnchor = {
                    "kind": "line",
                    "path": path.as_posix(),
                    "line_start": start,
                    "line_end": max(start, end_line),
                }
                chunks.append(
                    make_chunk(
                        representation_id=representation_id,
                        representation_hash=representation_hash,
                        ordinal=len(chunks),
                        text=content,
                        extractor=self.name,
                        extractor_version=self.version,
                        anchor=anchor,
                        title=heading,
                    )
                )
            buffer = []

        for line_number, line in enumerate(lines, start=1):
            match = _HEADING.match(line)
            if match and buffer:
                flush(line_number - 1)
                start = line_number
            if match:
                heading = match.group(2).strip()
            if not buffer:
                start = line_number
            buffer.append(line)
            if len(buffer) >= self.max_lines:
                flush(line_number)
                start = line_number + 1
        flush(len(lines))
        return ExtractionResult(
            representation_id=representation_id,
            source_path=str(path),
            representation_hash=representation_hash,
            extractor=self.name,
            extractor_version=self.version,
            status=(
                ExtractionStatus.SUCCEEDED
                if chunks
                else ExtractionStatus.PARTIAL
            ),
            chunks=tuple(chunks),
            warnings=() if chunks else ("Text file is empty",),
            metadata={"encoding": encoding, "line_count": len(lines)},
        )
