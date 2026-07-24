"""Citation construction and validation."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote, urlencode

from .models import Citation, RetrievedEvidence


def citation_from_evidence(item: RetrievedEvidence) -> Citation:
    document = item.hit.document
    representation_id = document.metadata.get("representation_id")
    anchor = document.metadata.get("anchor")
    representation = str(representation_id) if representation_id else None
    typed_anchor = dict(anchor) if isinstance(anchor, dict) else None
    path = f"/assets/{quote(document.asset_id)}/documents/{quote(document.document_id)}"
    query: dict[str, str | int] = {}
    fragment = ""
    if typed_anchor:
        kind = typed_anchor.get("kind")
        if kind in {"pdf", "pdf_page"} and typed_anchor.get("page") is not None:
            query["page"] = int(typed_anchor["page"])
        elif kind in {"html", "html_heading"}:
            dom_id = typed_anchor.get("dom_id") or typed_anchor.get("heading_id")
            if dom_id:
                fragment = quote(str(dom_id))
            elif typed_anchor.get("heading") or typed_anchor.get("heading_path"):
                query["heading"] = str(
                    typed_anchor.get("heading") or typed_anchor["heading_path"]
                )
        elif kind in {"line", "text_lines", "code_lines"}:
            query["path"] = str(typed_anchor.get("path", ""))
            query["line"] = int(
                typed_anchor.get("line_start", typed_anchor.get("line", 1))
            )
        elif kind in {"notebook", "notebook_cell"}:
            query["cell"] = int(typed_anchor.get("cell_index", 0))
        elif kind == "tabular":
            query["row"] = int(typed_anchor.get("row_start", 1))
    if query:
        path = f"{path}?{urlencode(query)}"
    if fragment:
        path = f"{path}#{fragment}"
    return Citation(
        citation_id=item.citation_id,
        asset_id=document.asset_id,
        document_id=document.document_id,
        representation_id=representation,
        anchor=typed_anchor,
        title=document.title,
        url=path,
        chunk_id=(
            str(document.metadata["chunk_id"])
            if document.metadata.get("chunk_id")
            else None
        ),
        chunk_hash=(
            str(document.metadata["chunk_hash"])
            if document.metadata.get("chunk_hash")
            else None
        ),
        representation_digest=(
            str(document.metadata["representation_digest"])
            if document.metadata.get("representation_digest")
            else None
        ),
    )


def select_citations(
    cited_ids: Sequence[str],
    evidence: Sequence[RetrievedEvidence],
) -> tuple[Citation, ...]:
    by_id = {item.citation_id: item for item in evidence}
    selected: list[Citation] = []
    for citation_id in cited_ids:
        item = by_id.get(citation_id)
        if item is None:
            raise ValueError(f"Provider cited unknown evidence: {citation_id}")
        if not item.hit.document.metadata.get("representation_id"):
            raise ValueError(f"Evidence has no openable representation: {citation_id}")
        selected.append(citation_from_evidence(item))
    return tuple(selected)
