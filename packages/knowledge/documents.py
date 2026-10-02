"""Pure document reconstruction and Markdown export; never reads source files."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_evidence_id(
    representation_id: str, source_hash: str, page: int | None, content_hash: str
) -> str:
    identity = f"{representation_id}:{source_hash}:{page}:{content_hash}"
    return "evidence:" + text_hash(identity)


def document_from_chunks(
    *,
    asset_id: str,
    representation_id: str,
    source_hash: str,
    title: str,
    source_type: str,
    chunks: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebuild pages from one representation's ordered chunks, preserving chunk IDs."""
    ordered = sorted(chunks, key=lambda item: (item["chunk_index"], str(item["id"])))
    paths = {(item.get("anchor") or {}).get("source_path") for item in ordered}
    if len(paths - {None, ""}) > 1:
        raise ValueError("A document cannot combine different source files.")
    groups: dict[int | None, list[Mapping[str, Any]]] = {}
    for chunk in ordered:
        anchor = chunk.get("anchor") or {}
        page = anchor.get("page") if anchor.get("kind") == "pdf" else None
        if page is not None and (type(page) is not int or page < 1):
            raise ValueError("PDF pages must be positive integers.")
        groups.setdefault(page, []).append(chunk)
    pages: list[dict[str, Any]] = []
    for page, members in groups.items():
        text = "\n\n".join(str(item["text_content"]) for item in members)
        digest = text_hash(text)
        anchor = dict(members[0].get("anchor") or {})
        # Bounding boxes from one chunk do not locate the whole reconstructed page.
        anchor.pop("bbox", None)
        anchor["precision"] = "page" if page is not None else "document"
        pages.append(
            {
                "page": page,
                "text": text,
                "content_hash": digest,
                "evidence_id": page_evidence_id(representation_id, source_hash, page, digest),
                "anchor": anchor,
                "chunk_ids": ["chunk:" + str(item["id"]) for item in members],
            }
        )
    pages.sort(key=lambda item: item["page"] or 0)
    return {
        "schema_version": 1,
        "asset_id": asset_id,
        "representation_id": representation_id,
        "source_hash": source_hash,
        "title": title,
        "source_type": source_type,
        "quality_status": "page_text" if all(p["page"] for p in pages) else "document_text",
        "pages": pages,
        "figures": [],
    }


def export_source_markdown(document: Mapping[str, Any]) -> str:
    if document.get("source_type", "original") != "original":
        raise ValueError("source.md requires original evidence.")
    return _export_markdown(document, "original")


def export_analysis_markdown(document: Mapping[str, Any]) -> str:
    if document.get("source_type") != "analysis":
        raise ValueError("analysis.zh.md requires analysis evidence.")
    return _export_markdown(document, "analysis")


def _export_markdown(document: Mapping[str, Any], source_type: str) -> str:
    lines = [
        f"# {document['title']}",
        "",
        f"<!-- source_type: {source_type} -->",
        f"<!-- asset_id: {document['asset_id']} -->",
        f"<!-- representation_id: {document['representation_id']} -->",
        f"<!-- source_hash: {document['source_hash']} -->",
        "",
    ]
    for page in document["pages"]:
        lines.extend(
            [
                f"<!-- evidence_id: {page['evidence_id']} -->",
                f"<!-- page: {page['page']} -->",
                page["text"],
                "",
            ]
        )
    return "\n".join(lines)
