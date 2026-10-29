"""Text decoding and deterministic chunk helpers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replacement"


def bounded_blocks(
    paragraphs: Iterable[str],
    *,
    max_chars: int = 4_000,
) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        cleaned = paragraph.strip()
        if not cleaned:
            continue
        if current and current_size + len(cleaned) + 2 > max_chars:
            blocks.append("\n\n".join(current))
            current = []
            current_size = 0
        if len(cleaned) <= max_chars:
            current.append(cleaned)
            current_size += len(cleaned) + 2
            continue
        if current:
            blocks.append("\n\n".join(current))
            current = []
            current_size = 0
        blocks.extend(
            cleaned[offset : offset + max_chars]
            for offset in range(0, len(cleaned), max_chars)
        )
    if current:
        blocks.append("\n\n".join(current))
    return blocks
