"""Noise-reduced HTML extraction using the Python standard library."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from .common import bounded_blocks, read_text
from .models import (
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    HTMLAnchor,
    hash_bytes,
    make_chunk,
)

_SPACE = re.compile(r"\s+")
_IGNORED = {"nav", "footer", "script", "style", "noscript", "form", "svg"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _ContentParser(HTMLParser):
    def __init__(self, *, preferred_only: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.preferred_only = preferred_only
        self.preferred_depth = 0
        self.ignored_depth = 0
        self.heading_tag: str | None = None
        self.heading_text: list[str] = []
        self.current_heading = "Document"
        self.current_dom_id: str | None = None
        self.current_text: list[str] = []
        self.sections: list[tuple[str, str | None, str]] = []

    def _included(self) -> bool:
        return self.ignored_depth == 0 and (
            not self.preferred_only or self.preferred_depth > 0
        )

    def _flush(self) -> None:
        text = _SPACE.sub(" ", " ".join(self.current_text)).strip()
        if text:
            self.sections.append((self.current_heading, self.current_dom_id, text))
        self.current_text = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        if lowered in {"main", "article"}:
            self.preferred_depth += 1
        if lowered in _IGNORED:
            self.ignored_depth += 1
        if lowered in _HEADINGS and self._included():
            self._flush()
            self.heading_tag = lowered
            self.heading_text = []
            self.current_dom_id = dict(attrs).get("id")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == self.heading_tag:
            heading = _SPACE.sub(" ", " ".join(self.heading_text)).strip()
            if heading:
                self.current_heading = heading
            self.heading_tag = None
            self.heading_text = []
        if lowered in _IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
        if lowered in {"main", "article"} and self.preferred_depth:
            self.preferred_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._included():
            return
        if self.heading_tag is not None:
            self.heading_text.append(data)
        else:
            self.current_text.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


class HTMLExtractor:
    name = "html"
    version = "1"

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() in {".html", ".htm"}

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        raw = path.read_bytes()
        representation_hash = hash_bytes(raw)
        text, encoding = read_text(path)
        preferred_only = bool(re.search(r"<(?:main|article)(?:\s|>)", text, re.I))
        parser = _ContentParser(preferred_only=preferred_only)
        try:
            parser.feed(text)
            parser.close()
        except Exception as exc:
            return ExtractionResult(
                representation_id=representation_id,
                source_path=str(path),
                representation_hash=representation_hash,
                extractor=self.name,
                extractor_version=self.version,
                status=ExtractionStatus.FAILED,
                warnings=(f"HTML parsing failed: {exc}",),
                metadata={"encoding": encoding},
            )

        chunks: list[ExtractionChunk] = []
        for heading, dom_id, section_text in parser.sections:
            for block in bounded_blocks([section_text]):
                anchor: HTMLAnchor = {"kind": "html", "heading": heading}
                if dom_id:
                    anchor["dom_id"] = dom_id
                    anchor["selector"] = f"#{dom_id}"
                chunks.append(
                    make_chunk(
                        representation_id=representation_id,
                        representation_hash=representation_hash,
                        ordinal=len(chunks),
                        text=block,
                        extractor=self.name,
                        extractor_version=self.version,
                        anchor=anchor,
                        title=heading,
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
                if chunks
                else ExtractionStatus.PARTIAL
            ),
            chunks=tuple(chunks),
            warnings=() if chunks else ("No indexable HTML body found",),
            metadata={
                "encoding": encoding,
                "preferred_container_used": preferred_only,
                "section_count": len(parser.sections),
            },
        )
