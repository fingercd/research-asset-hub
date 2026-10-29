#!/usr/bin/env python3
"""Extract paper identity, bibliographic metadata, and code leads offline.

The JSONL output is an append-only checkpoint journal.  Every asserted value in
``facts`` is backed by a candidate whose confidence met the configured fact
threshold.  Lower-confidence values remain visible only under ``candidates``.
Repository and project links are always leads: this offline tool cannot prove
that a repository is official.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pypdf
from pypdf import PdfReader

SCHEMA_VERSION = "paper-metadata/v2"
DEFAULT_FIRST_PAGES = 3
DEFAULT_FACT_THRESHOLD = 0.75
DEFAULT_MAX_TEXT_CHARS = 120_000
DEFAULT_HASH_CHUNK_SIZE = 8 * 1024 * 1024
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".cache",
        "cache",
        "caches",
        "weights",
        "checkpoints",
    }
)

DOI_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)"
    r"(10\.\d{4,9}/[-._;()/:A-Z0-9]*[A-Z0-9])"
    r"(?![-_/A-Z0-9]|\.(?=[-._;()/:A-Z0-9]))"
)
ARXIV_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(?:arxiv\s*:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)"
    r"((?:[a-z][a-z0-9.-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?"
    r"(?:\.pdf)?(?![A-Z0-9_/-]|\.(?=[A-Z0-9._/-]))"
)
URL_RE = re.compile(r"(?i)https?://[^\s<>{}\[\]\"']+")
TRUSTED_IDENTIFIER_METADATA_KEYS = {
    "doi": frozenset({"doi"}),
    "arxiv_id": frozenset({"arxiv", "arxiv_id", "arxivid"}),
}
YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-3]\d)(?!\d)")
ABSTRACT_RE = re.compile(
    r"(?is)(?:^|\n)\s*(?:abstract|summary)\s*[-—.:]?\s*(.{80,6000}?)"
    r"(?=\n\s*(?:1\s*[.]?\s*)?(?:introduction|keywords?|index terms?)\b|\Z)"
)
CODE_STATEMENT_RE = re.compile(
    r"(?is)(.{0,80}\b(?:source\s+code|code|implementation|project\s+(?:page|website))\b"
    r".{0,120}\b(?:available|released|found|at|on|from)\b.{0,220})"
)
VENUE_NAMES = (
    "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
    "Computer Vision and Pattern Recognition",
    "International Conference on Computer Vision",
    "European Conference on Computer Vision",
    "International Conference on Machine Learning",
    "International Conference on Learning Representations",
    "Neural Information Processing Systems",
    "Association for Computational Linguistics",
    "Empirical Methods in Natural Language Processing",
    "AAAI Conference on Artificial Intelligence",
    "International Joint Conference on Artificial Intelligence",
    "CVPR",
    "ICCV",
    "ECCV",
    "ICML",
    "ICLR",
    "NeurIPS",
    "ACL",
    "EMNLP",
    "AAAI",
    "IJCAI",
    "SIGGRAPH",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: object, *, limit: int = 20_000) -> str:
    """Convert a PDF value to bounded, log-safe text."""

    # Broken PDF font maps occasionally surface lone UTF-16 surrogate code
    # points.  They cannot be encoded into the UTF-8 JSONL/CSV contract, so
    # replace only those invalid code points while preserving normal Unicode.
    text = str(value).encode("utf-8", errors="replace").decode("utf-8").replace("\x00", " ")
    text = re.sub(r"[\t\r ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def excerpt(value: str, *, limit: int = 280) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def normalized_identity_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def evidence(source: str, location: str, value: str) -> dict[str, str]:
    return {
        "source": source,
        "location": location,
        "excerpt": excerpt(sanitize_urls_in_text(value)),
    }


def candidate(
    field: str,
    value: Any,
    confidence: float,
    reason: str,
    evidence_items: Sequence[dict[str, str]],
    *,
    fact_eligible: bool = True,
) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "reason": reason,
        "evidence": list(evidence_items),
        "fact_eligible": fact_eligible,
    }


def _dedupe_candidates(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_value: dict[str, dict[str, Any]] = {}
    for item in values:
        key = json.dumps(item["value"], ensure_ascii=False, sort_keys=True).casefold()
        previous = by_value.get(key)
        if previous is None:
            by_value[key] = item
            continue
        previous["evidence"] = list(
            {
                json.dumps(entry, ensure_ascii=False, sort_keys=True): entry
                for entry in [*previous["evidence"], *item["evidence"]]
            }.values()
        )
        previous["fact_eligible"] = bool(
            previous.get("fact_eligible", True) or item.get("fact_eligible", True)
        )
        if item["confidence"] > previous["confidence"]:
            previous["confidence"] = item["confidence"]
            previous["reason"] = item["reason"]
    return sorted(
        by_value.values(), key=lambda item: (-float(item["confidence"]), str(item["value"]))
    )


def _metadata_dict(reader: PdfReader) -> tuple[dict[str, str], list[str]]:
    warnings: list[str] = []
    result: dict[str, str] = {}
    try:
        metadata = reader.metadata
        if metadata:
            for key, value in metadata.items():
                if value is not None:
                    safe_key = sanitize_urls_in_text(clean_text(key, limit=200))
                    result[safe_key] = sanitize_urls_in_text(clean_text(value))
    except Exception as exc:  # pypdf exposes backend-specific malformed metadata errors
        warnings.append(
            "embedded_metadata_error:"
            f"{type(exc).__name__}:{sanitize_urls_in_text(clean_text(exc, limit=300))}"
        )
    return result, warnings


def _xmp_dict(reader: PdfReader) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    result: dict[str, Any] = {}
    try:
        xmp = reader.xmp_metadata
        if xmp is None:
            return result, warnings
        for name in (
            "dc_title",
            "dc_creator",
            "dc_description",
            "dc_subject",
            "xmp_create_date",
            "xmp_modify_date",
        ):
            try:
                value = getattr(xmp, name, None)
            except Exception as exc:
                warnings.append(f"xmp_field_error:{name}:{type(exc).__name__}")
                continue
            if value is not None:
                if isinstance(value, (list, tuple)):
                    result[name] = [
                        sanitize_urls_in_text(clean_text(item, limit=2_000)) for item in value
                    ]
                elif isinstance(value, dict):
                    result[name] = {
                        sanitize_urls_in_text(clean_text(key, limit=100)): sanitize_urls_in_text(
                            clean_text(item, limit=2_000)
                        )
                        for key, item in value.items()
                    }
                else:
                    result[name] = sanitize_urls_in_text(clean_text(value, limit=2_000))
    except Exception as exc:
        warnings.append(
            "xmp_metadata_error:"
            f"{type(exc).__name__}:{sanitize_urls_in_text(clean_text(exc, limit=300))}"
        )
    return result, warnings


def _extract_pages(
    reader: PdfReader, first_pages: int, max_text_chars: int
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    texts: list[str] = []
    page_stats: list[dict[str, Any]] = []
    warnings: list[str] = []
    remaining = max_text_chars
    for index in range(min(first_pages, len(reader.pages))):
        if remaining <= 0:
            warnings.append("text_limit_reached")
            break
        try:
            raw = reader.pages[index].extract_text() or ""
            text = clean_text(raw, limit=remaining)
            texts.append(text)
            page_stats.append({"page": index + 1, "characters": len(text), "status": "ok"})
            remaining -= len(text)
        except Exception as exc:
            texts.append("")
            page_stats.append(
                {"page": index + 1, "characters": 0, "status": "error", "error": type(exc).__name__}
            )
            warnings.append(f"page_text_error:{index + 1}:{type(exc).__name__}")
    return texts, page_stats, warnings


def _annotation_uris(reader: PdfReader) -> tuple[list[dict[str, Any]], list[str]]:
    found: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[tuple[int, str]] = set()
    for page_index, page in enumerate(reader.pages):
        try:
            annotations = page.get("/Annots") or []
            for annotation_ref in annotations:
                annotation = annotation_ref.get_object()
                action_ref = annotation.get("/A")
                action = (
                    action_ref.get_object() if hasattr(action_ref, "get_object") else action_ref
                )
                if not action or action.get("/S") != "/URI":
                    continue
                uri = clean_url(clean_text(action.get("/URI"), limit=4_000))
                if not uri or (page_index + 1, uri.casefold()) in seen:
                    continue
                seen.add((page_index + 1, uri.casefold()))
                item: dict[str, Any] = {"page": page_index + 1, "uri": uri}
                context_parts = [annotation.get(key) for key in ("/Contents", "/T", "/TU")]
                context = " ".join(
                    sanitize_urls_in_text(clean_text(part, limit=500))
                    for part in context_parts
                    if part
                )
                if context:
                    item["annotation_text"] = context
                found.append(item)
        except Exception as exc:
            warnings.append(f"annotation_error:{page_index + 1}:{type(exc).__name__}")
    return found, warnings


def clean_url(value: str) -> str:
    value = value.strip().rstrip(".,;:!?)]}>'\"")
    if not value.lower().startswith(("http://", "https://")):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except (UnicodeError, ValueError):
        return ""
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    netloc = f"[{host}]" if ":" in host else host
    # Credentials, ports, query parameters, and fragments are never needed for
    # bibliographic evidence and can contain passwords or signed access tokens.
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def sanitize_urls_in_text(value: str) -> str:
    """Remove credentials and query/fragment data from every visible HTTP URL."""

    return URL_RE.sub(lambda match: clean_url(match.group(0)) or "[invalid-url]", value)


def _metadata_value(metadata: dict[str, str], *names: str) -> str:
    folded = {key.casefold().lstrip("/"): value for key, value in metadata.items()}
    for name in names:
        value = folded.get(name.casefold().lstrip("/"))
        if value:
            return value
    return ""


def _xmp_text(xmp: dict[str, Any], name: str) -> str:
    value = xmp.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        preferred = value.get("x-default") or value.get("en")
        return str(preferred if preferred is not None else next(iter(value.values()), ""))
    return ""


def _looks_like_title(value: str) -> bool:
    folded = value.casefold().strip()
    return (
        8 <= len(value.strip()) <= 500
        and folded not in {"untitled", "document", "microsoft word", "arxiv"}
        and not folded.startswith(("http://", "https://"))
        and len(re.findall(r"[A-Za-z\u4e00-\u9fff]", value)) >= 5
    )


def _first_page_title(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip(" \t-—") for line in text.splitlines()]
    plausible: list[str] = []
    for line in lines[:30]:
        folded = line.casefold()
        if not _looks_like_title(line):
            continue
        if any(token in folded for token in ("arxiv:", "doi:", "abstract", "@", "http")):
            continue
        if YEAR_RE.fullmatch(line):
            continue
        plausible.append(line)
        if len(plausible) == 2:
            break
    if not plausible:
        return ""
    if len(plausible) == 2 and len(plausible[0]) < 85 and len(plausible[1]) < 85:
        combined = f"{plausible[0]} {plausible[1]}"
        if len(combined) <= 180:
            return combined
    return plausible[0]


def _split_authors(value: str) -> list[str]:
    value = re.sub(r"\s+", " ", value).strip(" ,;")
    if not value:
        return []
    if re.search(r"\b(?:anonymous|unknown|et al\.?|authors?)\b", value, re.I):
        return []
    parts = re.split(r"\s*(?:;|\||\band\b|\u00b7)\s*", value, flags=re.I)
    if len(parts) == 1 and value.count(",") >= 2:
        parts = [part.strip() for part in value.split(",")]
    return [part for part in parts if 2 <= len(part) <= 160][:100]


def _first_page_authors(text: str, title: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    title_index = 0
    title_key = normalized_identity_text(title)
    for index, line in enumerate(lines[:30]):
        if title_key and normalized_identity_text(line) in title_key:
            title_index = index
            break
    for line in lines[title_index + 1 : title_index + 7]:
        folded = line.casefold()
        if any(
            token in folded for token in ("university", "institute", "laboratory", "abstract", "@")
        ):
            continue
        if len(line) > 350 or re.search(r"https?://|\d{4}", line):
            continue
        names = _split_authors(line)
        if names and all(len(re.findall(r"[A-Za-z\u4e00-\u9fff]", name)) >= 2 for name in names):
            return names
    return []


def _identifier_candidates(
    field: str,
    pattern: re.Pattern[str],
    sources: Sequence[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for source_name, location, text in sources:
        for match in pattern.finditer(text):
            raw = match.group(1).rstrip(".,;:)]}")
            normalized = raw.casefold()
            if field == "arxiv_id":
                normalized = re.sub(r"(?i)v\d+$", "", normalized)
            page_match = re.fullmatch(r"page:(\d+)", location)
            page_number = int(page_match.group(1)) if page_match else None
            containing_url_match = next(
                (
                    url_match
                    for url_match in URL_RE.finditer(text)
                    if url_match.start() <= match.start() and url_match.end() >= match.end()
                ),
                None,
            )
            containing_url = (
                containing_url_match.group(0) if containing_url_match is not None else None
            )
            url_host = ""
            if containing_url:
                try:
                    url_host = (urlsplit(containing_url).hostname or "").casefold().rstrip(".")
                except (UnicodeError, ValueError):
                    url_host = ""
            match_is_whole_canonical_url = (
                containing_url_match is not None
                and match.start() == containing_url_match.start()
                and match.group(0).casefold().startswith(("http://", "https://"))
            )
            trusted_url_host = (field == "doi" and url_host in {"doi.org", "dx.doi.org"}) or (
                field == "arxiv_id" and (url_host == "arxiv.org" or url_host.endswith(".arxiv.org"))
            )
            trusted_url = match_is_whole_canonical_url and trusted_url_host
            metadata_key = location.casefold().lstrip("/")
            trusted_metadata = (
                source_name == "embedded_metadata"
                and metadata_key in TRUSTED_IDENTIFIER_METADATA_KEYS.get(field, frozenset())
            )
            if source_name == "annotation_uri":
                fact_eligible = page_number == 1 and trusted_url
                confidence = 0.96 if fact_eligible else 0.45
                reason = (
                    f"canonical {field} host linked on page one"
                    if fact_eligible
                    else f"untrusted or later-page {field} link is only a review candidate"
                )
            elif source_name == "first_pages_text":
                fact_eligible = page_number == 1 and (containing_url is None or trusted_url)
                confidence = 0.93 if fact_eligible else 0.58
                reason = (
                    f"explicit {field} pattern on page one"
                    if fact_eligible
                    else f"untrusted URL or later-page {field} may identify another work"
                )
            elif trusted_metadata:
                fact_eligible = containing_url is None or trusted_url
                confidence = 0.9 if fact_eligible else 0.55
                reason = (
                    f"explicit {field} pattern in its dedicated document metadata field"
                    if fact_eligible
                    else f"{field} appears inside an untrusted dedicated-metadata URL"
                )
            elif source_name in {"embedded_metadata", "xmp_metadata"}:
                fact_eligible = False
                confidence = 0.55
                reason = f"{field} in arbitrary document metadata is only a review candidate"
            elif source_name == "filename":
                fact_eligible = False
                confidence = 0.55
                reason = f"filename {field} is never sufficient identity evidence"
            else:
                fact_eligible = False
                confidence = 0.55
                reason = f"explicit {field} pattern with ambiguous document relevance"
            values.append(
                candidate(
                    field,
                    normalized,
                    confidence,
                    reason,
                    [evidence(source_name, location, match.group(0))],
                    fact_eligible=fact_eligible,
                )
            )
    return _dedupe_candidates(values)


def _venue_year_candidates(
    pages: Sequence[str], metadata: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    venues: list[dict[str, Any]] = []
    years: list[dict[str, Any]] = []
    for page_number, page_text in enumerate(pages, start=1):
        for line_number, line in enumerate(page_text.splitlines()[:180], start=1):
            folded = line.casefold()
            matched_venue = next((name for name in VENUE_NAMES if name.casefold() in folded), None)
            if not matched_venue:
                continue
            venue_evidence = evidence(
                "first_pages_text", f"page:{page_number}:line:{line_number}", line
            )
            if page_number == 1:
                venue_confidence = 0.84 if YEAR_RE.search(line) else 0.78
                venue_reason = "recognized venue name on page one"
            else:
                venue_confidence = 0.52
                venue_reason = "recognized venue name after page one may be a citation"
            venues.append(
                candidate(
                    "venue",
                    matched_venue,
                    venue_confidence,
                    venue_reason,
                    [venue_evidence],
                )
            )
            for year_match in YEAR_RE.finditer(line):
                years.append(
                    candidate(
                        "year",
                        int(year_match.group(1)),
                        0.84 if page_number == 1 else 0.5,
                        (
                            "year appears with a recognized venue on page one"
                            if page_number == 1
                            else "venue/year after page one may belong to a cited work"
                        ),
                        [venue_evidence],
                    )
                )
    date_value = _metadata_value(metadata, "CreationDate", "ModDate")
    date_match = YEAR_RE.search(date_value)
    if date_match:
        years.append(
            candidate(
                "year",
                int(date_match.group(1)),
                0.45,
                "PDF creation/modification date is not reliable publication evidence",
                [evidence("embedded_metadata", "/CreationDate or /ModDate", date_value)],
            )
        )
    return _dedupe_candidates(venues), _dedupe_candidates(years)


def _abstract_candidates(
    text: str, metadata: dict[str, str], xmp: dict[str, Any]
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    match = ABSTRACT_RE.search(text)
    if match:
        abstract = sanitize_urls_in_text(re.sub(r"\s+", " ", match.group(1)).strip())
        values.append(
            candidate(
                "abstract",
                abstract,
                0.9,
                "bounded text follows an explicit Abstract heading",
                [evidence("first_pages_text", "Abstract section", abstract)],
            )
        )
    metadata_subject = _metadata_value(metadata, "Subject", "Description")
    if len(metadata_subject) >= 80:
        values.append(
            candidate(
                "abstract",
                metadata_subject,
                0.68,
                "PDF subject/description may contain an abstract",
                [evidence("embedded_metadata", "/Subject or /Description", metadata_subject)],
            )
        )
    xmp_description = _xmp_text(xmp, "dc_description")
    if len(xmp_description) >= 80:
        values.append(
            candidate(
                "abstract",
                xmp_description,
                0.72,
                "XMP description may contain an abstract",
                [evidence("xmp_metadata", "dc_description", xmp_description)],
            )
        )
    return _dedupe_candidates(values)


def _canonical_repo_url(uri: str) -> tuple[str, str] | None:
    sanitized = clean_url(uri)
    if not sanitized:
        return None
    try:
        parsed = urlsplit(sanitized)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    safe_segment = re.compile(r"^[A-Za-z0-9_.-]+$")
    if host in {"www.github.com", "github.com"} and len(segments) >= 2:
        owner, repository = segments[:2]
        repository = repository.removesuffix(".git")
        if (
            repository.casefold() in {"issues", "pulls", "topics", "search"}
            or not safe_segment.fullmatch(owner)
            or not safe_segment.fullmatch(repository)
        ):
            return None
        return "repository", f"https://github.com/{owner}/{repository}"
    if host in {"www.gitlab.com", "gitlab.com"} and len(segments) >= 2:
        if "-" in segments:
            segments = segments[: segments.index("-")]
        if len(segments) < 2 or not all(safe_segment.fullmatch(segment) for segment in segments):
            return None
        repository = "/".join(segments).removesuffix(".git")
        return "repository", f"https://gitlab.com/{repository}"
    if host.endswith(".github.io"):
        return "project_page", sanitized
    return None


def _code_leads(
    text: str, annotation_uris: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    sources: list[tuple[str, str, str, float]] = []
    for item in annotation_uris:
        page_number = int(item["page"])
        sources.append(
            (
                "annotation_uri",
                f"page:{page_number}",
                str(item["uri"]),
                0.94 if page_number <= 2 else 0.52,
            )
        )
    for match in URL_RE.finditer(text):
        uri = clean_url(match.group(0))
        if uri:
            sources.append(("first_pages_text", f"char:{match.start()}", uri, 0.86))

    leads: list[dict[str, Any]] = []
    web_urls: set[str] = set()
    for source_name, location, uri, confidence in sources:
        web_urls.add(uri)
        classified = _canonical_repo_url(uri)
        if classified is None:
            continue
        lead_type, canonical_url = classified
        leads.append(
            {
                "type": lead_type,
                "url": canonical_url,
                "confidence": confidence,
                "status": "candidate_unverified",
                "reason": (
                    "repository host or project-site host recognized; "
                    "official ownership and relevance to this paper are not verified"
                ),
                "evidence": [evidence(source_name, location, uri)],
            }
        )

    for match in CODE_STATEMENT_RE.finditer(text):
        raw_statement = match.group(1)
        raw_statement_urls = list(URL_RE.finditer(raw_statement))
        trusted_statement_urls: list[str] = []

        def replace_statement_url(
            url_match: re.Match[str], trusted_urls: list[str] = trusted_statement_urls
        ) -> str:
            classified = _canonical_repo_url(url_match.group(0))
            if classified is None:
                return "[untrusted-url-omitted]"
            canonical_url = classified[1]
            trusted_urls.append(canonical_url)
            return canonical_url

        statement = excerpt(URL_RE.sub(replace_statement_url, raw_statement), limit=420)
        if raw_statement_urls and not trusted_statement_urls:
            # An arbitrary external URL next to the word "code" is not a code
            # lead.  Keep its sanitized form in the general web_urls inventory,
            # but do not let it enter the code-specific evidence domain.
            continue
        statement_urls = sorted(set(trusted_statement_urls))
        leads.append(
            {
                "type": "code_availability_statement",
                "value": statement,
                "urls": statement_urls,
                "confidence": 0.68 if statement_urls else 0.48,
                "status": "candidate_unverified",
                "reason": (
                    "paper text mentions code or a project page; "
                    "offline extraction cannot verify it"
                ),
                "evidence": [evidence("first_pages_text", f"char:{match.start()}", statement)],
            }
        )

    by_key: dict[str, dict[str, Any]] = {}
    for lead in leads:
        key = f"{lead['type']}\0{lead.get('url', lead.get('value', ''))}".casefold()
        previous = by_key.get(key)
        if previous is None or lead["confidence"] > previous["confidence"]:
            by_key[key] = lead
        elif previous is not None:
            previous["evidence"] = [*previous["evidence"], *lead["evidence"]]
    return sorted(
        by_key.values(), key=lambda item: (-float(item["confidence"]), item["type"])
    ), sorted(web_urls)


def _title_candidates(
    path: Path, first_page: str, metadata: dict[str, str], xmp: dict[str, Any]
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    metadata_title = _metadata_value(metadata, "Title")
    xmp_title = _xmp_text(xmp, "dc_title")
    page_title = _first_page_title(first_page)
    filename_title = re.sub(r"[_-]+", " ", path.stem).strip()

    if _looks_like_title(metadata_title):
        confidence = 0.88
        reason = "non-generic embedded PDF title"
        evidence_items = [evidence("embedded_metadata", "/Title", metadata_title)]
        if page_title and normalized_identity_text(page_title) == normalized_identity_text(
            metadata_title
        ):
            confidence = 0.97
            reason = "embedded title agrees with first-page title"
            evidence_items.append(
                evidence("first_pages_text", "page:1 title candidate", page_title)
            )
        values.append(candidate("title", metadata_title, confidence, reason, evidence_items))
    if _looks_like_title(xmp_title):
        confidence = 0.87
        if page_title and normalized_identity_text(page_title) == normalized_identity_text(
            xmp_title
        ):
            confidence = 0.96
        values.append(
            candidate(
                "title",
                xmp_title,
                confidence,
                "XMP title" + (" agrees with first page" if confidence > 0.9 else ""),
                [evidence("xmp_metadata", "dc_title", xmp_title)],
            )
        )
    if page_title:
        values.append(
            candidate(
                "title",
                page_title,
                0.64,
                "first plausible heading on page one; layout was not independently verified",
                [evidence("first_pages_text", "page:1 title candidate", page_title)],
            )
        )
    if _looks_like_title(filename_title):
        values.append(
            candidate(
                "title",
                filename_title,
                0.35,
                "filename is only a weak title hint",
                [evidence("filename", path.name, filename_title)],
            )
        )
    return _dedupe_candidates(values)


def _author_candidates(
    first_page: str,
    title_values: Sequence[dict[str, Any]],
    metadata: dict[str, str],
    xmp: dict[str, Any],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    metadata_author = _metadata_value(metadata, "Author")
    authors = _split_authors(metadata_author)
    if authors:
        values.append(
            candidate(
                "authors",
                authors,
                0.86,
                "non-generic embedded PDF author field",
                [evidence("embedded_metadata", "/Author", metadata_author)],
            )
        )
    xmp_creators = xmp.get("dc_creator")
    if isinstance(xmp_creators, list) and xmp_creators:
        creators = [
            clean_text(item, limit=160) for item in xmp_creators if clean_text(item, limit=160)
        ]
        if creators:
            values.append(
                candidate(
                    "authors",
                    creators,
                    0.88,
                    "XMP creator list",
                    [evidence("xmp_metadata", "dc_creator", "; ".join(creators))],
                )
            )
    title = str(title_values[0]["value"]) if title_values else ""
    page_authors = _first_page_authors(first_page, title)
    if page_authors:
        values.append(
            candidate(
                "authors",
                page_authors,
                0.56,
                "name-like line follows the first-page title candidate",
                [evidence("first_pages_text", "page:1 author candidate", "; ".join(page_authors))],
            )
        )
    return _dedupe_candidates(values)


def _facts(
    candidates: dict[str, list[dict[str, Any]]], threshold: float
) -> tuple[dict[str, Any], list[str]]:
    facts: dict[str, Any] = {}
    conflicts: list[str] = []
    singular = {"title", "authors", "venue", "year", "abstract"}
    for field, values in candidates.items():
        fact_eligible_default = field not in {"doi", "arxiv_id"}
        accepted = [
            item
            for item in values
            if bool(item.get("fact_eligible", fact_eligible_default))
            and float(item["confidence"]) >= threshold
        ]
        if not accepted:
            continue
        if field in singular:
            if len(accepted) > 1:
                conflicts.append(field)
                continue
            facts[field] = accepted[0]
        else:
            facts[field] = accepted
    return facts, conflicts


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(DEFAULT_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Fields that reveal replacement or mutation without relying on atime."""

    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _is_reparse_point(path: Path) -> bool:
    """Recognize Windows junctions as well as ordinary symbolic links."""

    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
        is_symlink = path.is_symlink()
    except (OSError, RuntimeError, ValueError):
        # Let the per-file extraction path report a visible error instead of
        # silently classifying an unreadable ordinary file as a link.
        return False
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return is_symlink or bool(attributes & reparse_flag)


def _first_reparse_component(path: Path) -> Path | None:
    """Return the first linked path component without resolving the input."""

    absolute = _absolute_path_without_resolving(path)
    anchor = Path(absolute.anchor)
    current = anchor
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            return current
    return None


def extraction_options_digest(first_pages: int, fact_threshold: float, max_text_chars: int) -> str:
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "first_pages": first_pages,
            "fact_threshold": fact_threshold,
            "max_text_chars": max_text_chars,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _absolute_path_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(str(path.expanduser())))


def _new_record(
    path: Path,
    *,
    first_pages: int,
    fact_threshold: float,
    max_text_chars: int,
) -> dict[str, Any]:
    absolute = _absolute_path_without_resolving(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "extracted_at": utc_now(),
        "source": {
            "path": str(absolute),
            "path_key": os.path.normcase(str(absolute)),
            "size": None,
            "mtime_ns": None,
            "sha256": None,
        },
        "extraction_options": {
            "first_pages": first_pages,
            "fact_threshold": fact_threshold,
            "max_text_chars": max_text_chars,
            "digest": extraction_options_digest(first_pages, fact_threshold, max_text_chars),
        },
        "status": "error",
        "parser": {"name": "pypdf", "version": pypdf.__version__},
        "embedded_metadata": {},
        "xmp_metadata": {},
        "annotation_uris": [],
        "web_urls": [],
        "candidates": {},
        "facts": {},
        "code_leads": [],
        "warnings": [],
    }


def extract_pdf(
    path: Path,
    *,
    first_pages: int = DEFAULT_FIRST_PAGES,
    fact_threshold: float = DEFAULT_FACT_THRESHOLD,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    precomputed_sha256: str | None = None,
) -> dict[str, Any]:
    """Extract a single PDF without network access or source writes."""

    record = _new_record(
        path,
        first_pages=first_pages,
        fact_threshold=fact_threshold,
        max_text_chars=max_text_chars,
    )
    stage = "reparse_check"
    try:
        unresolved = _absolute_path_without_resolving(path)
        linked_component = _first_reparse_component(unresolved)
        if linked_component is not None:
            raise ValueError(
                f"input contains a symlink, junction, or reparse point: {linked_component}"
            )
        stage = "resolve"
        resolved = unresolved.resolve(strict=True)
        stage = "stat"
        stat_result = resolved.stat()
        record["source"].update(
            {
                "path": str(resolved),
                "path_key": os.path.normcase(str(resolved)),
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
            }
        )
        initial_stat_fingerprint = _stat_fingerprint(stat_result)
        stage = "hash"
        initial_sha256 = precomputed_sha256 or hash_file(resolved)
        record["source"]["sha256"] = initial_sha256
        stage = "source_stability_preparse"
        post_hash_stat = resolved.stat()
        if (
            _stat_fingerprint(post_hash_stat) != initial_stat_fingerprint
            or _first_reparse_component(unresolved) is not None
        ):
            record["source"]["sha256"] = None
            record["warnings"].append("source_changed_during_initial_hash")
            raise RuntimeError("source changed while its initial fingerprint was computed")
        stage = "parse"
        reader = PdfReader(resolved, strict=False)
        if reader.is_encrypted:
            decrypt_result = reader.decrypt("")
            if not decrypt_result:
                record["warnings"].append("encrypted_pdf_requires_password")
                record["parser"].update({"encrypted": True, "page_count": None})
                record["error"] = {
                    "stage": "decrypt",
                    "type": "EncryptedPdfError",
                    "message": "PDF requires a non-empty password",
                }
                return record
        record["parser"].update(
            {"encrypted": bool(reader.is_encrypted), "page_count": len(reader.pages)}
        )
        metadata, metadata_warnings = _metadata_dict(reader)
        xmp, xmp_warnings = _xmp_dict(reader)
        pages, page_stats, page_warnings = _extract_pages(reader, first_pages, max_text_chars)
        annotation_uris, annotation_warnings = _annotation_uris(reader)
        record["embedded_metadata"] = metadata
        record["xmp_metadata"] = xmp
        record["annotation_uris"] = annotation_uris
        record["parser"]["page_text"] = page_stats
        record["warnings"].extend(
            [*metadata_warnings, *xmp_warnings, *page_warnings, *annotation_warnings]
        )

        first_page = pages[0] if pages else ""
        all_text = "\n".join(pages)
        titles = _title_candidates(resolved, first_page, metadata, xmp)
        authors = _author_candidates(first_page, titles, metadata, xmp)
        venues, years = _venue_year_candidates(pages, metadata)
        abstracts = _abstract_candidates(all_text, metadata, xmp)
        identifier_sources: list[tuple[str, str, str]] = [
            ("embedded_metadata", key, value) for key, value in metadata.items()
        ]
        identifier_sources.extend(
            ("xmp_metadata", key, json.dumps(value, ensure_ascii=False))
            for key, value in xmp.items()
        )
        identifier_sources.append(("filename", resolved.name, resolved.name))
        identifier_sources.extend(
            ("first_pages_text", f"page:{page_number}", page_text)
            for page_number, page_text in enumerate(pages, start=1)
        )
        identifier_sources.extend(
            ("annotation_uri", f"page:{item['page']}", str(item["uri"])) for item in annotation_uris
        )
        dois = _identifier_candidates("doi", DOI_RE, identifier_sources)
        arxiv_ids = _identifier_candidates("arxiv_id", ARXIV_RE, identifier_sources)
        candidates = {
            "title": titles,
            "authors": authors,
            "doi": dois,
            "arxiv_id": arxiv_ids,
            "venue": venues,
            "year": years,
            "abstract": abstracts,
        }
        code_leads, web_urls = _code_leads(all_text, annotation_uris)
        record["web_urls"] = web_urls
        record["candidates"] = candidates
        facts, fact_conflicts = _facts(candidates, fact_threshold)
        record["facts"] = facts
        record["warnings"].extend(f"fact_conflict:{field}" for field in fact_conflicts)
        record["code_leads"] = code_leads
        stage = "source_stability_postparse"
        final_sha256 = hash_file(resolved)
        final_stat = resolved.stat()
        if (
            final_sha256 != initial_sha256
            or _stat_fingerprint(final_stat) != initial_stat_fingerprint
            or _first_reparse_component(unresolved) is not None
        ):
            record["source"]["sha256"] = None
            record["embedded_metadata"] = {}
            record["xmp_metadata"] = {}
            record["annotation_uris"] = []
            record["web_urls"] = []
            record["candidates"] = {}
            record["facts"] = {}
            record["code_leads"] = []
            record["warnings"].append("source_changed_during_extraction")
            raise RuntimeError("source changed while it was being parsed")
        record["status"] = "ok"
    except Exception as exc:  # every input receives a visible outcome record
        record["error"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": sanitize_urls_in_text(clean_text(exc, limit=1_000)),
        }
    return record


def scan_inputs(inputs: Sequence[Path]) -> list[Path]:
    """Discover PDFs deterministically without following directory links."""

    found: dict[str, Path] = {}

    def remember(path: Path) -> None:
        absolute = _absolute_path_without_resolving(path)
        found[os.path.normcase(str(absolute))] = absolute

    for raw_path in inputs:
        unresolved = _absolute_path_without_resolving(raw_path)
        # The link check deliberately precedes resolve/stat/existence checks:
        # Path.exists() follows links and a Windows junction would otherwise
        # escape the caller-declared scan boundary before we reject it.
        linked_component = _first_reparse_component(unresolved)
        if linked_component is not None:
            raise ValueError(
                f"explicit input contains a symlink, junction, or reparse point: {linked_component}"
            )
        try:
            path = unresolved.resolve(strict=True)
            path_mode = path.stat().st_mode
        except (OSError, RuntimeError, ValueError):
            # A missing/inaccessible input is still an input.  Return its
            # lexical path so run() can append a status=error record while
            # continuing with the rest of the batch.
            remember(unresolved)
            continue
        if stat.S_ISREG(path_mode):
            if path.suffix.casefold() != ".pdf":
                raise ValueError(f"not a PDF: {path}")
            remember(path)
            continue
        if not stat.S_ISDIR(path_mode):
            raise ValueError(f"input is neither a PDF nor directory: {path}")

        def remember_walk_error(error: OSError, root: Path = path) -> None:
            failing_path = Path(error.filename) if error.filename else root
            remember(failing_path)

        walker = os.walk(path, followlinks=False, onerror=remember_walk_error)
        while True:
            try:
                current, dir_names, file_names = next(walker)
            except StopIteration:
                break
            except (OSError, RuntimeError, ValueError):
                remember(path)
                break
            current_path = Path(current)
            dir_names[:] = sorted(
                name
                for name in dir_names
                if name.casefold() not in EXCLUDED_DIR_NAMES
                and not _is_reparse_point(current_path / name)
            )
            for name in sorted(file_names):
                candidate_path = current_path / name
                if candidate_path.suffix.casefold() == ".pdf" and not _is_reparse_point(
                    candidate_path
                ):
                    remember(candidate_path)
    return [found[key] for key in sorted(found)]


def _read_journal(
    path: Path, *, repair_truncated_tail: bool = True
) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    raw = path.read_text(encoding="utf-8")
    has_unterminated_tail = bool(raw) and not raw.endswith(("\n", "\r"))
    lines = raw.splitlines()
    records: list[dict[str, Any]] = []
    invalid_tail = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and has_unterminated_tail and repair_truncated_tail:
                invalid_tail = 1
                break
            raise ValueError(f"invalid JSONL at line {index + 1}: {path}") from None
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {index + 1} is not an object: {path}")
        records.append(value)
    if invalid_tail:
        _atomic_write_text(
            path, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        )
    return records, invalid_tail


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(value, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open("rb") as binary:
            binary.seek(-1, os.SEEK_END)
            needs_newline = binary.read(1) not in {b"\n", b"\r"}
    else:
        needs_newline = False
    with path.open("a", encoding="utf-8", newline="") as handle:
        if needs_newline:
            handle.write("\n")
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _latest_by_path(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        source = record.get("source")
        if not isinstance(source, dict):
            continue
        path_key = source.get("path_key")
        if isinstance(path_key, str):
            latest[path_key] = record
    return latest


def _fact_value(record: dict[str, Any], field: str) -> Any:
    value = record.get("facts", {}).get(field)
    if isinstance(value, dict):
        return value.get("value")
    if isinstance(value, list):
        return [item.get("value") for item in value if isinstance(item, dict)]
    return None


def _fact_confidence(record: dict[str, Any], field: str) -> Any:
    value = record.get("facts", {}).get(field)
    if isinstance(value, dict):
        return value.get("confidence")
    if isinstance(value, list):
        return [item.get("confidence") for item in value if isinstance(item, dict)]
    return None


CSV_FIELDS = (
    "source_path",
    "size",
    "mtime_ns",
    "sha256",
    "status",
    "page_count",
    "title",
    "title_confidence",
    "authors_json",
    "authors_confidence",
    "year",
    "year_confidence",
    "venue",
    "venue_confidence",
    "arxiv_ids_json",
    "dois_json",
    "abstract",
    "abstract_confidence",
    "code_leads_json",
    "warnings_json",
    "error_json",
)


def _csv_row(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source", {})
    parser = record.get("parser", {})
    return {
        "source_path": source.get("path"),
        "size": source.get("size"),
        "mtime_ns": source.get("mtime_ns"),
        "sha256": source.get("sha256"),
        "status": record.get("status"),
        "page_count": parser.get("page_count"),
        "title": _fact_value(record, "title"),
        "title_confidence": _fact_confidence(record, "title"),
        "authors_json": json.dumps(_fact_value(record, "authors"), ensure_ascii=False),
        "authors_confidence": _fact_confidence(record, "authors"),
        "year": _fact_value(record, "year"),
        "year_confidence": _fact_confidence(record, "year"),
        "venue": _fact_value(record, "venue"),
        "venue_confidence": _fact_confidence(record, "venue"),
        "arxiv_ids_json": json.dumps(_fact_value(record, "arxiv_id"), ensure_ascii=False),
        "dois_json": json.dumps(_fact_value(record, "doi"), ensure_ascii=False),
        "abstract": _fact_value(record, "abstract"),
        "abstract_confidence": _fact_confidence(record, "abstract"),
        "code_leads_json": json.dumps(record.get("code_leads", []), ensure_ascii=False),
        "warnings_json": json.dumps(record.get("warnings", []), ensure_ascii=False),
        "error_json": json.dumps(record.get("error"), ensure_ascii=False),
    }


def _write_csv(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for record in records:
                writer.writerow(_csv_row(record))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _output_paths(output_prefix: Path) -> tuple[Path, Path]:
    prefix = (
        output_prefix.with_suffix("")
        if output_prefix.suffix.casefold() in {".jsonl", ".csv"}
        else output_prefix
    )
    return Path(f"{prefix}.jsonl"), Path(f"{prefix}.csv")


def run(
    inputs: Sequence[Path],
    output_prefix: Path,
    *,
    first_pages: int = DEFAULT_FIRST_PAGES,
    fact_threshold: float = DEFAULT_FACT_THRESHOLD,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    force: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    pdfs = scan_inputs(inputs)
    jsonl_path, csv_path = _output_paths(output_prefix)
    if overwrite:
        _atomic_write_text(jsonl_path, "")
    journal, repaired_lines = _read_journal(jsonl_path)
    latest = _latest_by_path(journal)
    options_digest = extraction_options_digest(first_pages, fact_threshold, max_text_chars)
    processed = 0
    skipped = 0
    processed_failures = 0
    current_keys: set[str] = set()
    for pdf in pdfs:
        path_key = os.path.normcase(str(_absolute_path_without_resolving(pdf)))
        current_keys.add(path_key)
        record: dict[str, Any] | None = None
        stage = "reparse_check"
        try:
            linked_component = _first_reparse_component(pdf)
            if linked_component is not None:
                raise ValueError(
                    "input became or entered a symlink, junction, or reparse point: "
                    f"{linked_component}"
                )
            stage = "resolve"
            resolved = pdf.resolve(strict=True)
            stage = "stat"
            stat_result = resolved.stat()
            path_key = os.path.normcase(str(resolved))
            current_keys.add(path_key)
            previous = latest.get(path_key)
            previous_source = previous.get("source", {}) if previous else {}
            previous_options = previous.get("extraction_options", {}) if previous else {}
            resume_candidate = (
                previous is not None
                and previous_source.get("size") == stat_result.st_size
                and previous_source.get("mtime_ns") == stat_result.st_mtime_ns
                and previous_options.get("digest") == options_digest
            )
            precomputed_sha256: str | None = None
            if resume_candidate and not force:
                stage = "resume_hash"
                first_sha256 = hash_file(resolved)
                middle_stat = resolved.stat()
                second_sha256 = hash_file(resolved)
                final_stat = resolved.stat()
                linked_component = _first_reparse_component(pdf)
                stable_resume_source = (
                    linked_component is None
                    and _stat_fingerprint(stat_result) == _stat_fingerprint(middle_stat)
                    and _stat_fingerprint(middle_stat) == _stat_fingerprint(final_stat)
                    and first_sha256 == second_sha256
                )
                if stable_resume_source:
                    precomputed_sha256 = second_sha256
                if stable_resume_source and previous_source.get("sha256") == second_sha256:
                    skipped += 1
                    continue
            stage = "extract"
            record = extract_pdf(
                resolved,
                first_pages=first_pages,
                fact_threshold=fact_threshold,
                max_text_chars=max_text_chars,
                precomputed_sha256=precomputed_sha256,
            )
        except Exception as exc:
            record = _new_record(
                pdf,
                first_pages=first_pages,
                fact_threshold=fact_threshold,
                max_text_chars=max_text_chars,
            )
            record["error"] = {
                "stage": stage,
                "type": type(exc).__name__,
                "message": sanitize_urls_in_text(clean_text(exc, limit=1_000)),
            }
        assert record is not None
        record_key = str(record["source"]["path_key"])
        current_keys.add(record_key)
        _append_jsonl(jsonl_path, record)
        journal.append(record)
        latest[record_key] = record
        processed += 1
        processed_failures += int(record["status"] != "ok")
    current = [latest[key] for key in sorted(current_keys) if key in latest]
    _write_csv(csv_path, current)
    current_failures = sum(record.get("status") != "ok" for record in current)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "inputs_discovered": len(pdfs),
        "processed": processed,
        "skipped_unchanged": skipped,
        "processed_failures": processed_failures,
        "failures": current_failures,
        "repaired_truncated_jsonl_lines": repaired_lines,
        "journal_records": len(journal),
        "current_records": len(current),
        "jsonl": str(jsonl_path.resolve()),
        "csv": str(csv_path.resolve()),
    }
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline, resumable extraction of paper metadata and code leads from PDFs."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF files and/or directories to scan")
    parser.add_argument(
        "--output-prefix",
        required=True,
        type=Path,
        help="Output prefix; .jsonl checkpoint journal and .csv snapshot are written",
    )
    parser.add_argument("--first-pages", type=int, default=DEFAULT_FIRST_PAGES)
    parser.add_argument("--fact-threshold", type=float, default=DEFAULT_FACT_THRESHOLD)
    parser.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS)
    parser.add_argument(
        "--force", action="store_true", help="reprocess all discovered PDFs and append revisions"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace the existing output journal"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.first_pages < 1:
        raise SystemExit("--first-pages must be at least 1")
    if not 0.0 <= args.fact_threshold <= 1.0:
        raise SystemExit("--fact-threshold must be between 0 and 1")
    if args.max_text_chars < 1_000:
        raise SystemExit("--max-text-chars must be at least 1000")
    try:
        summary = run(
            args.inputs,
            args.output_prefix,
            first_pages=args.first_pages,
            fact_threshold=args.fact_threshold,
            max_text_chars=args.max_text_chars,
            force=args.force,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError) as exc:
        print(f"paper-metadata: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
