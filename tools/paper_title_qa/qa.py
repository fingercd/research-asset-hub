"""Auditable first-page title/author extraction for paper PDFs.

The module intentionally produces QA candidates rather than silently promoting every
first-page string to a bibliographic fact.  Font/coordinate evidence is extracted with
pypdf; ``pdftotext -layout`` is used as a second, optional rendering of the first page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pypdf import PdfReader

SCHEMA_VERSION = "paper-title-qa/v1"
DEFAULT_ACCEPT_THRESHOLD = 0.78
DEFAULT_MARGIN_THRESHOLD = 0.06
MAX_FIRST_PAGE_TEXT = 80_000

SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")
YEAR_PREFIX_RE = re.compile(
    r"^(?:(?:ACL|EMNLP|NAACL|EACL|COLING|CVPR|ICCV|ECCV|ICLR|ICML|NEURIPS|NIPS|AAAI|IJCAI|"
    r"SIGGRAPH|KDD|WWW|CHI|IROS|ICRA)\s+)?(?:19|20)\d{2}\s+",
    re.IGNORECASE,
)
STATUS_SUFFIX_RE = re.compile(r"\s*[（(](?:已读|未读)[）)]\s*$", re.IGNORECASE)
SECTION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\s+)?(?:abstract|introduction|references)\b", re.I)
AFFILIATION_RE = re.compile(
    r"\b(?:university|institute|laborator(?:y|ies)|department|school|college|academy|research|"
    r"correspondence|equal contribution|work done|company|corporation|inc\.?|email)\b|@",
    re.IGNORECASE,
)
BOILERPLATE_PATTERNS = (
    re.compile(r"\bopen access version\b", re.I),
    re.compile(r"\bcomputer vision foundation\b", re.I),
    re.compile(r"\bfinal published version\b", re.I),
    re.compile(r"\bproceedings (?:of|is|are|available)\b", re.I),
    re.compile(r"\bieee xplore\b", re.I),
    re.compile(r"\bpreprint\b.*\barxiv\b", re.I),
    re.compile(r"^arxiv:\s*\d", re.I),
    re.compile(r"^accepted (?:at|by|to)\b", re.I),
    re.compile(r"^published as a conference paper\b", re.I),
    re.compile(r"^under review\b", re.I),
    re.compile(r"^proceedings of the\b", re.I),
    re.compile(r"^©\s*\d{4}\b"),
)
METHOD_PATTERNS = (
    re.compile(
        r"\b(?:we|this work)\s+(?:propose|introduce|present|develop)\s+(?:a|an|the)?\s*"
        r"([A-Z][A-Za-z0-9.+-]{2,}(?:\s*\([A-Z][A-Z0-9-]{1,12}\))?)"
    ),
    re.compile(r"\bcalled\s+([A-Z][A-Za-z0-9.+-]{2,})"),
)


@dataclass(frozen=True)
class TextLine:
    """A reconstructed first-page text line."""

    text: str
    font_size: float
    x0: float
    y: float
    x1: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "font_size": round(self.font_size, 3),
            "x0": round(self.x0, 3),
            "y": round(self.y, 3),
            "x1": round(self.x1, 3),
        }


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\x00", " ")
    return SPACE_RE.sub(" ", text).strip()


def _plain_markdown(value: str) -> str:
    value = TAG_RE.sub("", value)
    value = re.sub(r"!\[[^]]*]\([^)]*\)", "", value)
    value = value.replace("\\*", "*")
    return _clean_text(value)


def _normal_form(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("–", "-").replace("—", "-")
    return SPACE_RE.sub(" ", re.sub(r"[^\w]+", " ", value)).strip()


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in WORD_RE.findall(value) if len(token) > 1}


def _similarity(left: str, right: str) -> float:
    a = _normal_form(left)
    b = _normal_form(right)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    left_tokens = _tokens(a)
    right_tokens = _tokens(b)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    containment = 0.0
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 12 and (longer.startswith(shorter) or shorter in longer):
        containment = min(1.0, len(shorter) / max(1, len(longer)) + 0.18)
    return round(max(seq, jaccard, containment), 4)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _join_fragments(fragments: Sequence[tuple[float, str]]) -> str:
    result = ""
    for _, raw in sorted(fragments, key=lambda item: item[0]):
        text = _clean_text(raw)
        if not text:
            continue
        if not result:
            result = text
        elif text[0] in ",.;:!?)]}%" or result.endswith(("(", "[", "{", "/", "-")):
            result += text
        else:
            result += " " + text
    return _clean_text(result)


def _visible_lines(page: Any) -> tuple[list[TextLine], float, float]:
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    fragments: list[tuple[str, float, float, float]] = []

    def visitor(
        text: str,
        _cm: Sequence[float],
        tm: Sequence[float],
        _font_dict: dict[str, Any] | None,
        font_size: float,
    ) -> None:
        clean = _clean_text(text)
        if not clean or len(tm) < 6:
            return
        x = float(tm[4])
        y = float(tm[5])
        size = abs(float(font_size))
        if not all(math.isfinite(number) for number in (x, y, size)):
            return
        # PDF figures often carry text in transformed coordinate systems far outside the page.
        if not (-2 <= x <= width + 2 and -2 <= y <= height + 2 and 2 <= size <= 96):
            return
        fragments.append((clean, size, x, y))

    page.extract_text(visitor_text=visitor)
    buckets: list[list[tuple[str, float, float, float]]] = []
    for fragment in sorted(fragments, key=lambda item: (-item[3], item[2])):
        matching = next(
            (bucket for bucket in buckets if abs(bucket[0][3] - fragment[3]) <= 2.4),
            None,
        )
        if matching is None:
            buckets.append([fragment])
        else:
            matching.append(fragment)

    lines: list[TextLine] = []
    for bucket in buckets:
        ordered = sorted(bucket, key=lambda item: item[2])
        text = _join_fragments([(item[2], item[0]) for item in ordered])
        if not text:
            continue
        weighted = sum(item[1] * max(1, len(item[0])) for item in ordered)
        weight = sum(max(1, len(item[0])) for item in ordered)
        x0 = min(item[2] for item in ordered)
        x1 = max(item[2] + item[1] * 0.52 * len(item[0]) for item in ordered)
        lines.append(TextLine(text, weighted / weight, x0, bucket[0][3], min(width, x1)))
    return sorted(lines, key=lambda line: (-line.y, line.x0)), width, height


def _is_boilerplate(text: str) -> str | None:
    clean = _clean_text(text)
    for pattern in BOILERPLATE_PATTERNS:
        if pattern.search(clean):
            return pattern.pattern
    return None


def _body_font(lines: Sequence[TextLine], height: float) -> float:
    counter: Counter[float] = Counter()
    for line in lines:
        if line.y > height * 0.82 or len(line.text) < 25:
            continue
        counter[round(line.font_size * 2) / 2] += min(240, len(line.text))
    if not counter:
        for line in lines:
            counter[round(line.font_size * 2) / 2] += min(120, len(line.text))
    return counter.most_common(1)[0][0] if counter else 10.0


def _title_blocks(
    lines: Sequence[TextLine], width: float, height: float
) -> tuple[list[dict], list[dict]]:
    body_font = _body_font(lines, height)
    abstract_y = next(
        (line.y for line in lines if _normal_form(line.text) == "abstract"),
        height * 0.40,
    )
    rejected: list[dict] = []
    eligible: list[TextLine] = []
    for line in lines:
        reason = _is_boilerplate(line.text)
        if reason and line.y >= height * 0.70:
            rejected.append({"text": line.text, "reason": "boilerplate_header", "pattern": reason})
            continue
        normalized = _normal_form(line.text)
        if (
            line.y <= abstract_y + 8
            or line.y < height * 0.48
            or line.y > height * 0.97
            or len(normalized) < 8
            or len(normalized) > 260
            or SECTION_RE.match(normalized)
            or line.font_size < max(10.5, body_font * 1.13)
            or len(_tokens(line.text)) < 2
        ):
            continue
        eligible.append(line)

    eligible.sort(key=lambda line: -line.y)
    groups: list[list[TextLine]] = []
    for line in eligible:
        if not groups:
            groups.append([line])
            continue
        previous = groups[-1][-1]
        gap = previous.y - line.y
        size_ratio = max(previous.font_size, line.font_size) / max(
            0.1, min(previous.font_size, line.font_size)
        )
        if 0 <= gap <= max(30.0, previous.font_size * 2.1) and size_ratio <= 1.18:
            groups[-1].append(line)
        else:
            groups.append([line])

    blocks: list[dict] = []
    for group in groups:
        text = _clean_text(" ".join(line.text for line in group))
        if not (12 <= len(text) <= 350 and len(_tokens(text)) >= 3):
            continue
        average_font = sum(line.font_size * len(line.text) for line in group) / sum(
            len(line.text) for line in group
        )
        center = (min(line.x0 for line in group) + max(line.x1 for line in group)) / 2
        center_score = max(0.0, 1 - abs(center - width / 2) / (width / 2))
        font_score = min(1.0, max(0.0, average_font / max(body_font, 0.1) - 1.0) / 0.55)
        y_ratio = max(line.y for line in group) / height
        y_score = max(0.0, 1 - abs(y_ratio - 0.84) / 0.24)
        raw_score = 0.46 + 0.17 * font_score + 0.09 * center_score + 0.08 * y_score
        blocks.append(
            {
                "value": text,
                "source": "pypdf_layout",
                "base_confidence": round(min(0.80, raw_score), 4),
                "evidence": {
                    "lines": [line.as_dict() for line in group],
                    "body_font_size": body_font,
                    "font_dominance": round(average_font / max(body_font, 0.1), 3),
                    "abstract_y": round(abstract_y, 3),
                },
            }
        )
    return sorted(blocks, key=lambda item: item["base_confidence"], reverse=True), rejected


def _author_layout_candidate(
    lines: Sequence[TextLine], title: dict | None, height: float
) -> dict[str, Any] | None:
    if title is None:
        return None
    title_lines = title["evidence"]["lines"]
    title_bottom = min(float(line["y"]) for line in title_lines)
    abstract_y = float(title["evidence"]["abstract_y"])
    candidates: list[TextLine] = []
    for line in lines:
        if not (abstract_y + 10 < line.y < title_bottom - 5):
            continue
        clean = _clean_text(line.text)
        if (
            not clean
            or _is_boilerplate(clean)
            or AFFILIATION_RE.search(clean)
            or len(clean) > 300
            or len(_tokens(clean)) < 2
        ):
            continue
        words = WORD_RE.findall(clean)
        uppercase_starts = sum(word[0].isupper() for word in words)
        if not words or uppercase_starts / len(words) < 0.45:
            continue
        candidates.append(line)
    if not candidates:
        return None
    candidates.sort(key=lambda line: -line.y)
    # Author lists commonly wrap once; stop before a large vertical separation.
    selected = [candidates[0]]
    for line in candidates[1:]:
        if selected[-1].y - line.y > max(25, selected[-1].font_size * 1.8):
            break
        selected.append(line)
    value = _clean_text(" ".join(line.text for line in selected))
    value = re.sub(r"(?<=\w)\s+[0-9*†‡,]+(?=\s|$)", "", value)
    return {
        "value": value,
        "source": "pypdf_layout",
        "base_confidence": 0.68,
        "evidence": {"lines": [line.as_dict() for line in selected]},
    }


def _pdftotext_layout(path: Path, executable: str | None) -> dict[str, Any]:
    if executable is None:
        return {"available": False, "lines": [], "error": "pdftotext_not_found"}
    try:
        completed = subprocess.run(
            [executable, "-f", "1", "-l", "1", "-layout", "-enc", "UTF-8", str(path), "-"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "lines": [], "error": f"{type(exc).__name__}: {exc}"}
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-500:]
        return {"available": False, "lines": [], "error": f"exit_{completed.returncode}: {stderr}"}
    text = completed.stdout.decode("utf-8", errors="replace")[:MAX_FIRST_PAGE_TEXT]
    lines = [_clean_text(line) for line in text.splitlines()]
    return {"available": True, "lines": [line for line in lines if line], "error": None}


def _filename_title(path: Path, supplied: Any) -> str:
    if supplied:
        return _clean_text(supplied)
    name = STATUS_SUFFIX_RE.sub("", path.stem).strip()
    name = YEAR_PREFIX_RE.sub("", name)
    name = _clean_text(name.replace("_", ": ", 1))
    return name


def _metadata_candidates(reader: PdfReader, supplied: dict[str, Any]) -> tuple[str, str]:
    metadata = reader.metadata or {}
    title = _clean_text(supplied.get("metadata_title") or getattr(metadata, "title", ""))
    authors = _clean_text(supplied.get("metadata_authors") or getattr(metadata, "author", ""))
    return title, authors


def _mineru_evidence(path: Path | None) -> dict[str, str]:
    if path is None:
        return {"title": "", "authors": "", "path": ""}
    text = path.read_text(encoding="utf-8", errors="strict")
    lines = text.splitlines()
    title = ""
    title_index = -1
    for index, line in enumerate(lines[:240]):
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match and not SECTION_RE.match(_normal_form(match.group(1))):
            title = _plain_markdown(match.group(1))
            title_index = index
            break
    authors = ""
    if title_index >= 0:
        for line in lines[title_index + 1 : title_index + 10]:
            clean = _plain_markdown(line)
            if not clean or clean.startswith("!"):
                continue
            if re.match(r"^#{1,6}\s+", line):
                break
            authors = clean
            break
    return {"title": title, "authors": authors, "path": str(path)}


def _abstract_text(reader: PdfReader) -> str:
    parts: list[str] = []
    for page in reader.pages[:2]:
        parts.append(page.extract_text() or "")
    text = _clean_text(" ".join(parts))[:MAX_FIRST_PAGE_TEXT]
    match = re.search(
        r"\babstract\b(.{0,7000}?)(?:\b(?:1\s+)?introduction\b|\n\s*1\s+)", text, re.I
    )
    return _clean_text(match.group(1) if match else text[:3500])


def _method_names(abstract: str) -> list[str]:
    names: list[str] = []
    for pattern in METHOD_PATTERNS:
        for match in pattern.finditer(abstract):
            name = _clean_text(match.group(1))
            if name and name.casefold() not in {item.casefold() for item in names}:
                names.append(name)
    return names[:12]


def _candidate(
    value: str, source: str, base: float, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "value": _clean_text(value),
        "source": source,
        "base_confidence": round(base, 4),
        "evidence": evidence or {},
    }


def _rank_title_candidates(
    raw_candidates: Sequence[dict[str, Any]],
    pdftotext: dict[str, Any],
    methods: Sequence[str],
    accept_threshold: float,
    margin_threshold: float,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_candidates:
        value = _clean_text(raw.get("value"))
        key = (raw.get("source", "unknown"), _normal_form(value))
        if not value or key in seen:
            continue
        seen.add(key)
        score = float(raw.get("base_confidence", 0.3))
        agreements: list[dict[str, Any]] = []
        disagreements: list[dict[str, Any]] = []
        for other in raw_candidates:
            if other is raw or other.get("source") == raw.get("source"):
                continue
            similarity = _similarity(value, _clean_text(other.get("value")))
            entry = {"source": other.get("source"), "similarity": similarity}
            value_normal = _normal_form(value)
            other_normal = _normal_form(_clean_text(other.get("value")))
            filename_prefix_agreement = other.get("source") == "filename" and (
                value_normal.startswith(other_normal) or other_normal.startswith(value_normal)
            )
            if similarity >= 0.82 or (filename_prefix_agreement and similarity >= 0.68):
                agreements.append(entry)
                bonus = {
                    "mineru_h1": 0.16,
                    "pdf_metadata": 0.12,
                    "filename": 0.08,
                    "pypdf_layout": 0.12,
                    "reference_title": 0.14,
                }.get(str(other.get("source")), 0.05)
                score += bonus * similarity
            elif similarity < 0.38 and float(other.get("base_confidence", 0)) >= 0.62:
                disagreements.append(entry)
        layout_lines = pdftotext.get("lines", []) if pdftotext.get("available") else []
        layout_join = " ".join(layout_lines[:12])
        layout_similarity = _similarity(value, layout_join)
        if _normal_form(value) in _normal_form(layout_join) or layout_similarity >= 0.55:
            score += 0.04
            agreements.append({"source": "pdftotext_layout", "similarity": layout_similarity})
        matched_methods = [name for name in methods if _similarity(name, value) >= 0.42]
        if matched_methods:
            score += min(0.06, 0.03 * len(matched_methods))
        candidate = dict(raw)
        candidate.update(
            {
                "confidence": round(min(0.99, score), 4),
                "agreements": agreements,
                "disagreements": disagreements,
                "abstract_method_matches": matched_methods,
            }
        )
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["confidence"], reverse=True)
    top = candidates[0] if candidates else None
    runner_up = candidates[1] if len(candidates) > 1 else None
    margin = (top["confidence"] - runner_up["confidence"]) if top and runner_up else 1.0
    accepted = None
    decision = "no_candidate"
    if top:
        if top["confidence"] < accept_threshold:
            decision = "review_low_confidence"
        elif margin < margin_threshold and _similarity(top["value"], runner_up["value"]) < 0.82:
            decision = "review_ambiguous"
        else:
            accepted = top["value"]
            decision = "accepted_high_confidence"
    return {
        "accepted": accepted,
        "decision": decision,
        "accept_threshold": accept_threshold,
        "margin": round(margin, 4),
        "candidates": candidates,
    }


def _rank_author_candidates(
    raw_candidates: Sequence[dict[str, Any]], accept_threshold: float
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates:
        value = _clean_text(raw.get("value"))
        if not value:
            continue
        score = float(raw.get("base_confidence", 0.3))
        agreements: list[dict[str, Any]] = []
        for other in raw_candidates:
            if other is raw or other.get("source") == raw.get("source"):
                continue
            similarity = _similarity(value, _clean_text(other.get("value")))
            if similarity >= 0.55:
                score += 0.10 * similarity
                agreements.append({"source": other.get("source"), "similarity": similarity})
        candidate = dict(raw)
        candidate.update({"confidence": round(min(0.96, score), 4), "agreements": agreements})
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["confidence"], reverse=True)
    top = candidates[0] if candidates else None
    accepted = top["value"] if top and top["confidence"] >= accept_threshold else None
    return {
        "accepted": accepted,
        "decision": "accepted_high_confidence" if accepted else "review_low_confidence",
        "accept_threshold": accept_threshold,
        "candidates": candidates,
    }


def _conflicts(
    title: dict[str, Any], authors: dict[str, Any], rejected_headers: Sequence[dict]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    candidates = title.get("candidates", [])
    if candidates:
        chosen = candidates[0]
        for candidate in candidates[1:]:
            if candidate.get("source") == chosen.get("source"):
                continue
            source_requires_visibility = candidate.get("source") in {
                "filename",
                "pdf_metadata",
                "mineru_h1",
                "reference_title",
            }
            similarity = _similarity(chosen["value"], candidate["value"])
            conflict_threshold = 0.68 if source_requires_visibility else 0.38
            if (
                candidate.get("base_confidence", 0) >= 0.60 or source_requires_visibility
            ) and similarity < conflict_threshold:
                conflicts.append(
                    {
                        "field": "title",
                        "kind": "strong_source_disagreement",
                        "chosen_source": chosen["source"],
                        "chosen_value": chosen["value"],
                        "other_source": candidate["source"],
                        "other_value": candidate["value"],
                        "similarity": similarity,
                    }
                )
    if authors.get("accepted") is None and len(authors.get("candidates", [])) > 1:
        conflicts.append({"field": "authors", "kind": "unresolved_author_candidates"})
    for rejected in rejected_headers:
        conflicts.append(
            {
                "field": "title",
                "kind": "rejected_header",
                "value": rejected["text"],
                "reason": rejected["reason"],
            }
        )
    return conflicts


def analyze_record(
    item: dict[str, Any],
    *,
    base_dir: Path,
    accept_threshold: float = DEFAULT_ACCEPT_THRESHOLD,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
    pdftotext_executable: str | None = None,
) -> dict[str, Any]:
    """Analyze one JSONL item and return a complete auditable record.

    Exceptions intentionally propagate to the batch boundary, which emits a per-file error
    record.  This keeps filesystem/PDF failures from aborting unrelated inputs.
    """

    raw_path = item.get("pdf_path") or item.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("input item requires non-empty pdf_path")
    candidate_path = Path(raw_path)
    if not candidate_path.is_absolute():
        candidate_path = base_dir / candidate_path
    path = candidate_path.resolve(strict=True)
    if not path.is_file() or path.suffix.casefold() != ".pdf":
        raise ValueError(f"not a PDF file: {path}")
    source_sha256 = _sha256(path)
    reader = PdfReader(str(path), strict=False)
    if not reader.pages:
        raise ValueError("PDF has no pages")
    if reader.is_encrypted:
        result = reader.decrypt("")
        if not result:
            raise ValueError("encrypted PDF cannot be opened without a password")

    lines, width, height = _visible_lines(reader.pages[0])
    layout_titles, rejected_headers = _title_blocks(lines, width, height)
    author_layout = _author_layout_candidate(
        lines, layout_titles[0] if layout_titles else None, height
    )
    metadata_title, metadata_authors = _metadata_candidates(reader, item)
    filename_title = _filename_title(path, item.get("filename_title"))

    mineru_path: Path | None = None
    if item.get("mineru_path"):
        mineru_candidate = Path(str(item["mineru_path"]))
        if not mineru_candidate.is_absolute():
            mineru_candidate = base_dir / mineru_candidate
        mineru_path = mineru_candidate.resolve(strict=True)
        if not mineru_path.is_file():
            raise ValueError(f"not a MinerU Markdown file: {mineru_path}")
    mineru = _mineru_evidence(mineru_path)
    abstract = _abstract_text(reader)
    methods = _method_names(abstract)
    rendered = _pdftotext_layout(path, pdftotext_executable)

    raw_title_candidates: list[dict[str, Any]] = list(layout_titles[:5])
    if metadata_title:
        raw_title_candidates.append(_candidate(metadata_title, "pdf_metadata", 0.66))
    if filename_title:
        raw_title_candidates.append(
            _candidate(filename_title, "filename", 0.48, {"may_be_truncated": len(path.stem) >= 85})
        )
    if mineru["title"]:
        raw_title_candidates.append(
            _candidate(mineru["title"], "mineru_h1", 0.72, {"path": mineru["path"]})
        )
    if item.get("reference_title"):
        raw_title_candidates.append(
            _candidate(str(item["reference_title"]), "reference_title", 0.70)
        )
    title = _rank_title_candidates(
        raw_title_candidates, rendered, methods, accept_threshold, margin_threshold
    )

    raw_author_candidates: list[dict[str, Any]] = []
    if author_layout:
        raw_author_candidates.append(author_layout)
    if metadata_authors:
        raw_author_candidates.append(_candidate(metadata_authors, "pdf_metadata", 0.64))
    if mineru["authors"]:
        raw_author_candidates.append(
            _candidate(mineru["authors"], "mineru_after_h1", 0.62, {"path": mineru["path"]})
        )
    authors = _rank_author_candidates(raw_author_candidates, accept_threshold)
    stat = path.stat()
    return {
        "schema_version": SCHEMA_VERSION,
        "record_key": str(item.get("id") or path),
        "status": "ok",
        "source": {
            "pdf_path": str(path),
            "sha256": source_sha256,
            "size": stat.st_size,
            "pages": len(reader.pages),
            "mineru_path": str(mineru_path) if mineru_path else None,
        },
        "title": title,
        "authors": authors,
        "abstract_method_names": methods,
        "conflicts": _conflicts(title, authors, rejected_headers),
        "layout": {
            "page_width": width,
            "page_height": height,
            "visible_line_count": len(lines),
            "rejected_headers": rejected_headers,
            "pdftotext": rendered,
        },
    }


def _options_digest(
    accept_threshold: float, margin_threshold: float, executable: str | None
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "accept_threshold": accept_threshold,
        "margin_threshold": margin_threshold,
        "pdftotext": bool(executable),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _read_journal(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    data = path.read_bytes()
    if not data:
        return [], False
    has_final_newline = data.endswith(b"\n")
    raw_lines = data.splitlines()
    records: list[dict[str, Any]] = []
    repaired = False
    for index, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            is_unterminated_tail = index == len(raw_lines) and not has_final_newline
            if not is_unterminated_tail:
                raise ValueError(f"invalid checkpoint JSONL at line {index}: {exc}") from exc
            repaired = True
            break
        if not isinstance(parsed, dict):
            raise ValueError(f"invalid checkpoint JSONL object at line {index}")
        records.append(parsed)
    if repaired:
        with path.open("wb") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")
    return records, repaired


def _append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _iter_input(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("JSON value is not an object")
            except (json.JSONDecodeError, ValueError) as exc:
                yield line_number, None, f"{type(exc).__name__}: {exc}"
                continue
            yield line_number, item, None


def run_batch(
    input_path: Path,
    output_path: Path,
    *,
    accept_threshold: float = DEFAULT_ACCEPT_THRESHOLD,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
    force: bool = False,
    pdftotext_executable: str | None = None,
) -> dict[str, Any]:
    input_path = input_path.resolve(strict=True)
    output_path = output_path.resolve(strict=False)
    prior, repaired = _read_journal(output_path)
    options_digest = _options_digest(accept_threshold, margin_threshold, pdftotext_executable)
    latest: dict[str, dict[str, Any]] = {}
    for record in prior:
        latest[str(record.get("record_key", ""))] = record
    counts = Counter()
    counts["checkpoint_tail_repaired"] = int(repaired)
    for line_number, item, parse_error in _iter_input(input_path):
        if parse_error:
            record = {
                "schema_version": SCHEMA_VERSION,
                "record_key": f"input-line:{line_number}",
                "status": "error",
                "input_line": line_number,
                "error": {"type": "InputJSONError", "message": parse_error},
                "options_digest": options_digest,
            }
            _append_record(output_path, record)
            counts["error"] += 1
            continue
        assert item is not None
        raw_path = str(item.get("pdf_path") or item.get("path") or "")
        record_key = str(item.get("id") or raw_path or f"input-line:{line_number}")
        try:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = input_path.parent / candidate
            resolved = candidate.resolve(strict=True)
            current_sha256 = _sha256(resolved)
            old = latest.get(record_key)
            if (
                not force
                and old
                and old.get("status") == "ok"
                and old.get("source", {}).get("sha256") == current_sha256
                and old.get("options_digest") == options_digest
            ):
                counts["skipped_unchanged"] += 1
                continue
            record = analyze_record(
                item,
                base_dir=input_path.parent,
                accept_threshold=accept_threshold,
                margin_threshold=margin_threshold,
                pdftotext_executable=pdftotext_executable,
            )
            record["record_key"] = record_key
            record["input_line"] = line_number
            record["options_digest"] = options_digest
            counts["ok"] += 1
        except Exception as exc:  # noqa: BLE001 - per-file boundary is intentional
            record = {
                "schema_version": SCHEMA_VERSION,
                "record_key": record_key,
                "status": "error",
                "input_line": line_number,
                "source": {"pdf_path": raw_path},
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "options_digest": options_digest,
            }
            counts["error"] += 1
        _append_record(output_path, record)
        latest[record_key] = record
    return {"input": str(input_path), "output": str(output_path), "counts": dict(counts)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_jsonl", type=Path, help="JSONL items with pdf_path and optional mineru_path"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="append-only QA/checkpoint JSONL"
    )
    parser.add_argument("--accept-threshold", type=float, default=DEFAULT_ACCEPT_THRESHOLD)
    parser.add_argument("--margin-threshold", type=float, default=DEFAULT_MARGIN_THRESHOLD)
    parser.add_argument(
        "--force", action="store_true", help="append a fresh revision even when SHA/options match"
    )
    parser.add_argument(
        "--no-pdftotext",
        action="store_true",
        help="disable optional pdftotext -layout corroboration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0 <= args.accept_threshold <= 1:
        raise SystemExit("--accept-threshold must be between 0 and 1")
    if not 0 <= args.margin_threshold <= 1:
        raise SystemExit("--margin-threshold must be between 0 and 1")
    executable = None if args.no_pdftotext else shutil.which("pdftotext")
    try:
        summary = run_batch(
            args.input_jsonl,
            args.output,
            accept_threshold=args.accept_threshold,
            margin_threshold=args.margin_threshold,
            force=args.force,
            pdftotext_executable=executable,
        )
    except (OSError, ValueError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["counts"].get("error", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
