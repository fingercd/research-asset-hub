"""Deterministic, cacheable VLN extraction using the existing local tools."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json

PDF_EXTRACTOR_VERSION = "pdftotext-layout+pdfplumber+pypdf/v4"
EXTRACTOR_VERSION = "pdftotext-layout+pdfplumber+pypdf/v5"
LOCAL_LAYOUT_REASON = (
    "User chose the existing local pdftotext, pdfplumber and pypdf tools; "
    "do not install or use MinerU."
)
HEADING_PATTERN = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(abstract|introduction|related\s+work|background|method(?:ology)?|approach|"
    r"experiments?|evaluation|results?|ablation(?:\s+stud(?:y|ies))?|discussion|"
    r"limitations?|conclusion(?:s)?|dataset|benchmark)(?:\s*$|\s{3,})",
    re.IGNORECASE,
)
METRIC_PATTERN = re.compile(
    r"(?:\b(?:SR|SPL|NE|OSR|nDTW|SDTW|RGS|RGSPL|success\s+rate)\b|\d+(?:\.\d+)?\s*%)",
    re.IGNORECASE,
)
TABLE_MARKER = re.compile(r"\btable\s+[0-9ivx]+\b", re.IGNORECASE)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr", "br", "figcaption"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr", "figcaption"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_text(value: str) -> str:
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _visible_html_text(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return _clean_text("".join(parser.parts))


def _read_document(path: Path, representation_type: str) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _visible_html_text(raw) if representation_type.startswith("html") else _clean_text(raw)
    return {
        "type": representation_type,
        "sha256": _sha256(path),
        "characters": len(text),
        "text": text[:60_000],
        "truncated": len(text) > 60_000,
    }


def _resolve(registry: SourceRegistry, representation: Mapping[str, Any]) -> Path:
    return registry.resolve(
        str(representation["source_alias"]),
        str(representation["root_key"]),
        str(representation["path_ref"]),
    )


def _representation_input(
    registry: SourceRegistry, representation: Mapping[str, Any]
) -> dict[str, Any]:
    """Fingerprint current files, not the possibly stale database SHA or state."""
    item = {
        "representation_id": representation["id"],
        "type": representation["type"],
        "source_alias": representation.get("source_alias"),
        "root_key": representation.get("root_key"),
        "path_ref": representation.get("path_ref"),
    }
    if not item["source_alias"] or not item["path_ref"]:
        return {**item, "local_state": "external_or_unresolved", "sha256": None}
    try:
        path = _resolve(registry, representation)
        state = "file" if path.is_file() else "directory" if path.is_dir() else "missing"
        return {**item, "local_state": state, "sha256": _sha256(path) if state == "file" else None}
    except (OSError, ValueError) as error:
        return {
            **item,
            "local_state": "unreadable",
            "sha256": None,
            "error_type": type(error).__name__,
        }


def _result(report: Mapping[str, Any], report_path: Path, *, cache_state: str) -> dict[str, Any]:
    return {
        "asset_id": report["asset_id"],
        "status": "cached" if cache_state == "cached" else report["status"],
        "extraction_status": report["status"],
        "cache_state": cache_state,
        "report": str(report_path),
        "page_count": report["pdf"]["page_count"],
        "text_characters": report["text"]["characters"],
        "table_count": sum(table["status"] == "extracted" for table in report["tables"]),
        "empty_pages": len(report["text"]["empty_pages"]),
    }


def _page_sections(pages: Sequence[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        for index, line in enumerate(lines):
            match = HEADING_PATTERN.match(line)
            if match is None:
                continue
            excerpt = _clean_text("\n".join(lines[index + 1 : index + 45]))[:4_000]
            found.append(
                {
                    "heading": _clean_text(line),
                    "kind": match.group(1).lower().replace(" ", "_"),
                    "page": page_number,
                    "excerpt": excerpt,
                }
            )
    return found


def _metric_evidence(pages: Sequence[str]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        for index, line in enumerate(lines):
            if METRIC_PATTERN.search(line) is None:
                continue
            context = _clean_text(" ".join(lines[max(0, index - 1) : index + 2]))
            if len(context) < 30 or context.casefold() in seen:
                continue
            seen.add(context.casefold())
            evidence.append({"page": page_number, "text": context[:700]})
            if len(evidence) >= 20:
                return evidence
    return evidence


def _table_pages(pages: Sequence[str]) -> list[int]:
    return [index for index, text in enumerate(pages) if TABLE_MARKER.search(text) is not None][:10]


def _extract_tables(pdf_path: Path, page_indices: Sequence[int]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    if not page_indices:
        return tables
    with pdfplumber.open(pdf_path) as document:
        for page_index in page_indices:
            if page_index >= len(document.pages):
                continue
            try:
                extracted = document.pages[page_index].extract_tables()
            except Exception as error:
                tables.append(
                    {
                        "page": page_index + 1,
                        "status": "failed",
                        "error_type": type(error).__name__,
                    }
                )
                continue
            for table_index, table in enumerate(extracted[:3], start=1):
                rows = [
                    [(_clean_text(cell) if cell else "") for cell in row[:16]] for row in table[:50]
                ]
                if not any(any(cell for cell in row) for row in rows):
                    continue
                tables.append(
                    {
                        "page": page_index + 1,
                        "table_index": table_index,
                        "status": "extracted",
                        "rows": rows,
                        "truncated": len(table) > 50,
                    }
                )
    return tables


def _pdf_metadata(path: Path) -> dict[str, Any]:
    reader = PdfReader(path)
    metadata: Mapping[str, Any] = reader.metadata or {}
    return {
        "page_count": len(reader.pages),
        "encrypted": reader.is_encrypted,
        "title": str(metadata.get("/Title") or "")[:500],
        "author": str(metadata.get("/Author") or "")[:500],
    }


def _run_pdftotext(pdf_path: Path, output_path: Path, executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "-layout", "-enc", "UTF-8", str(pdf_path), str(output_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not output_path.is_file():
        raise RuntimeError(
            f"pdftotext failed with exit code {result.returncode}: {result.stderr.strip()[:300]}"
        )
    return result.stderr.strip()[:2_000]


def _run_pdftotext_per_page(
    pdf_path: Path,
    *,
    page_count: int,
    executable: Path,
) -> tuple[list[str], list[str]]:
    pages: list[str] = []
    warnings: list[str] = []
    for page_number in range(1, page_count + 1):
        result = subprocess.run(
            [
                str(executable),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-layout",
                "-enc",
                "UTF-8",
                str(pdf_path),
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdftotext page {page_number} failed with exit code {result.returncode}"
            )
        pages.append(_clean_text(result.stdout.replace("\f", "\n")))
        if result.stderr.strip():
            warnings.append(f"page {page_number}: {result.stderr.strip()[:300]}")
    return pages, warnings


def _extract_paper(
    row: Mapping[str, Any],
    *,
    registry: SourceRegistry,
    output_root: Path,
    pdftotext: Path,
    force: bool,
) -> dict[str, Any]:
    asset_id = str(row["asset_id"])
    paper_root = output_root / asset_id
    paper_root.mkdir(parents=True, exist_ok=True)
    report_path = paper_root / "extraction.json"
    layout_path = paper_root / "layout.txt"
    primary_pdf = row.get("primary_pdf")
    if not isinstance(primary_pdf, Mapping):
        raise RuntimeError(f"paper {asset_id} has no primary PDF")
    pdf_path = _resolve(registry, primary_pdf)
    pdf_sha256 = _sha256(pdf_path)
    inputs: dict[str, Any] = {
        "pdf": _representation_input(registry, primary_pdf),
        "existing_documents": sorted(
            (_representation_input(registry, item) for item in row.get("existing_documents", [])),
            key=lambda item: str(item["representation_id"]),
        ),
    }
    cached: dict[str, Any] = {}
    if not force and report_path.is_file() and layout_path.is_file():
        cached = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            cached.get("extractor", {}).get("version") == EXTRACTOR_VERSION
            and cached.get("inputs") == inputs
            and cached.get("title") == row.get("title")
            and cached.get("text", {}).get("layout_sha256") == _sha256(layout_path)
        ):
            return _result(cached, report_path, cache_state="cached")

    cached_extractor = cached.get("extractor", {})
    cached_pdf_version = cached_extractor.get("pdf_version", cached_extractor.get("version"))
    cached_layout_sha256 = cached.get("text", {}).get("layout_sha256")
    reuse_pdf = (
        bool(cached)
        and cached_pdf_version == PDF_EXTRACTOR_VERSION
        and cached.get("pdf", {}).get("sha256") == pdf_sha256
        and (cached_layout_sha256 is None or cached_layout_sha256 == _sha256(layout_path))
    )
    if reuse_pdf:
        metadata = cached["pdf"]
        layout_text = layout_path.read_text(encoding="utf-8", errors="replace")
        pages = [_clean_text(page) for page in layout_text.split("\f")]
        if pages and not pages[-1]:
            pages.pop()
        pdf_warnings = [
            warning
            for warning in cached.get("pdf_warnings", cached.get("warnings", []))
            if not warning.startswith("existing document")
        ]
        recovery_notes = [
            f"{note}; recovered per-page output has {len(pages)} pages matching the PDF."
            if note.startswith("pdftotext page split count differs")
            and len(pages) == metadata["page_count"]
            else note
            for note in cached.get("recovery_notes", [])
        ]
        tables = cached["tables"]
    else:
        metadata = _pdf_metadata(pdf_path)
        pdf_warnings = []
        recovery_notes = []
        pdftotext_warning = _run_pdftotext(pdf_path, layout_path, pdftotext)
        if pdftotext_warning:
            pdf_warnings.append(pdftotext_warning)
        layout_text = layout_path.read_text(encoding="utf-8", errors="replace")
        pages = [_clean_text(page) for page in layout_text.split("\f")]
        if pages and not pages[-1]:
            pages.pop()
        tables = []
    if len(pages) != metadata["page_count"]:
        original_page_splits = len(pages)
        pages, page_warnings = _run_pdftotext_per_page(
            pdf_path,
            page_count=int(metadata["page_count"]),
            executable=pdftotext,
        )
        pdf_warnings.extend(page_warnings)
        recovery_notes.append(
            f"Recovered page boundaries by extracting each PDF page separately: "
            f"{original_page_splits} initial splits -> {len(pages)} pages; "
            f"pypdf reports {metadata['page_count']} pages."
        )
        layout_text = "\n\f\n".join(pages) + "\n"
        layout_path.write_text(layout_text, encoding="utf-8", newline="\n")
        reuse_pdf = False
    if not reuse_pdf:
        tables = _extract_tables(pdf_path, _table_pages(pages))
    empty_pages = [index for index, page in enumerate(pages, start=1) if len(page) < 20]
    warnings = list(pdf_warnings)
    existing_documents: list[dict[str, Any]] = []
    for representation in inputs["existing_documents"]:
        if representation.get("local_state") != "file":
            if representation["local_state"] != "external_or_unresolved":
                warnings.append(
                    f"existing document unavailable: {representation['representation_id']}:"
                    f"{representation['local_state']}"
                )
            continue
        try:
            path = _resolve(registry, representation)
            document = _read_document(path, str(representation["type"]))
            document.update(representation)
            existing_documents.append(document)
        except OSError as error:
            warnings.append(
                "existing document read failed: "
                f"{representation['representation_id']}:{type(error).__name__}"
            )

    report = {
        "schema": "research-asset-hub.vln-paper-extraction/v1",
        "status": (
            "passed"
            if pages
            and not empty_pages
            and not warnings
            and all(table["status"] == "extracted" for table in tables)
            else "warning"
        ),
        "asset_id": asset_id,
        "title": row.get("title"),
        "inputs": inputs,
        "extractor": {
            "requested": "local-layout",
            "used": "local-layout",
            "version": EXTRACTOR_VERSION,
            "pdf_version": PDF_EXTRACTOR_VERSION,
            "reason": LOCAL_LAYOUT_REASON,
            "limitations": [
                "Formula structure is not guaranteed.",
                "Complex multi-column tables can require PDF cross-checking.",
                "Scanned pages are not OCRed by these local tools.",
            ],
        },
        "pdf": {
            **metadata,
            "sha256": pdf_sha256,
            "size_bytes": pdf_path.stat().st_size,
            "representation_id": primary_pdf["id"],
            "type": primary_pdf["type"],
            "source_alias": primary_pdf.get("source_alias"),
            "root_key": primary_pdf.get("root_key"),
            "path_ref": primary_pdf.get("path_ref"),
        },
        "text": {
            "layout_path": "layout.txt",
            "layout_sha256": _sha256(layout_path),
            "characters": len(layout_text),
            "page_splits": len(pages),
            "empty_pages": empty_pages,
        },
        "sections": _page_sections(pages),
        "metric_evidence": _metric_evidence(pages),
        "tables": tables,
        "existing_documents": existing_documents,
        "recovery_notes": recovery_notes,
        "pdf_warnings": pdf_warnings,
        "warnings": warnings,
    }
    atomic_write_json(report_path, report)
    return _result(
        report, report_path, cache_state="documents_refreshed" if reuse_pdf else "extracted"
    )


def extract_inventory(
    inventory: Mapping[str, Any],
    *,
    registry: SourceRegistry,
    output_root: Path,
    workers: int = 4,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise RuntimeError("pdftotext executable is required for local-layout extraction")
    pdftotext = Path(executable).resolve()
    raw_items = inventory.get("items", [])
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise TypeError("inventory items must be a sequence")
    items = [item for item in raw_items if isinstance(item, Mapping)]
    if limit is not None:
        items = items[:limit]
    if not items:
        raise RuntimeError("inventory contains no papers to extract")
    output_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                _extract_paper,
                item,
                registry=registry,
                output_root=output_root,
                pdftotext=pdftotext,
                force=force,
            ): str(item.get("asset_id"))
            for item in items
        }
        for future in as_completed(futures):
            asset_id = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                errors.append(
                    {
                        "asset_id": asset_id,
                        "error_type": type(error).__name__,
                        "message": str(error)[:500],
                    }
                )

    results.sort(key=lambda item: str(item["asset_id"]))
    errors.sort(key=lambda item: item["asset_id"])
    status_counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    report = {
        "schema": "research-asset-hub.vln-extraction-batch/v1",
        "status": (
            "failed"
            if errors or len(results) != len(items)
            else "warning"
            if any(item["extraction_status"] != "passed" for item in results)
            else "passed"
        ),
        "requested": len(items),
        "completed": len(results),
        "errors": errors,
        "status_counts": dict(sorted(status_counts.items())),
        "extractor": {
            "requested": "local-layout",
            "used": "local-layout",
            "version": EXTRACTOR_VERSION,
            "pdf_version": PDF_EXTRACTOR_VERSION,
            "reason": LOCAL_LAYOUT_REASON,
            "pdftotext": str(pdftotext),
        },
        "totals": {
            "pages": sum(int(item.get("page_count") or 0) for item in results),
            "text_characters": sum(int(item.get("text_characters") or 0) for item in results),
            "tables": sum(int(item.get("table_count") or 0) for item in results),
            "empty_pages": sum(int(item.get("empty_pages") or 0) for item in results),
        },
        "items": results,
    }
    atomic_write_json(output_root / "batch-report.json", report)
    return report
