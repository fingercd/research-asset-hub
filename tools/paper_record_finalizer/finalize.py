"""Produce the final, evidence-linked paper-record set for the 2026-08-31 batch.

The tool is intentionally a *derived-record* builder.  It reads prior approval
artifacts and PDF evidence but never changes an intake manifest, a source PDF,
or a Hub configuration file.  Its hard contract is the 376-paper set: every
input overlay must identify exactly the same set and each resulting record must
have a non-empty title, authors, year, venue and abstract representation.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pypdf import PdfReader

EXPECTED_COUNT = 376
PUBLIC_STATUS = "official_public"
NON_PUBLIC_STATUSES = frozenset({"announced_pending", "none_found", "third_party_only"})
ALL_CODE_STATUSES = frozenset({PUBLIC_STATUS, *NON_PUBLIC_STATUSES})
SCHEMA_VERSION = "paper-record-final/v1"
LOCAL_CODE_AUDIT_SCHEMA_VERSION = "paper-record-final/v2-local-code-audit"

# These are evidence-driven exceptions, rather than a broad fuzzy-title rule.
# In particular, source file names can be truncated and are never authoritative
# just because they are longer than a PDF title block.
PRIMARY_PDF_TITLE_IDS = frozenset({"1-1-2-2025", "beyond-variance-2026"})
AGENT_TITLE_IDS = frozenset(
    {"cut-your-losses-learning-to-prune-paths-early-2026", "text-s-2-q-vdit-2025"}
)
EMCOMPRESS_ID = "emcompress-2026"
ADAVIDEORAG_ID = "arxiv-2506-13589"

SPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"[A-Za-z0-9]+")
SMALLCAPS_SPACED_RE = re.compile(r"\b(?:[A-Z]\s+){1,}[A-Z](?:[A-Z])?\b")
TRUNCATED_TITLE_END_RE = re.compile(
    r"(?i)(?:\b(?:and|or|for|via|with|to|of|in|the|a|an|from|by|on|at|as|into|toward|towards)|[-,:/])$"
)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSONL {path}: {exc}") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"JSONL must contain non-empty object rows: {path}")
    return rows


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _map_unique(
    rows: Iterable[dict[str, Any]], field: str, label: str
) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} row has no {field}")
        if value in mapped:
            raise ValueError(f"duplicate {field} in {label}: {value}")
        mapped[value] = row
    return mapped


def _normal_title(value: Any) -> str:
    """Normalize layout-only small-cap spacing without guessing title words."""
    # NFC preserves meaningful typographic math in paper titles (for example
    # S²Q-VDiT); compatibility folding would silently turn it into S2Q-VDiT.
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = SPACE_RE.sub(" ", text).strip(" .;:")
    # PDF text commonly emits e.g. ``S PEC VLM`` or ``M EMORY``.  Only join an
    # isolated capital with an all-cap fragment; ``A Framework`` remains intact.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\b([A-Z])\s+([A-Z]{2,})\b", r"\1\2", text)
        text = re.sub(r"\b([A-Z]{2,})\s+([A-Z])\b", r"\1\2", text)
        text = re.sub(r"\b([A-Z])\s+([A-Z])\b", r"\1\2", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*:\s*", ": ", text)
    text = re.sub(r"\s*/\s*", "/", text)
    return text.strip(" .;:")


def _title_key(value: str) -> str:
    return "".join(WORD_RE.findall(_normal_title(value).casefold()))


def _is_token_prefix(left: str, right: str) -> bool:
    left_tokens = WORD_RE.findall(_normal_title(left).casefold())
    right_tokens = WORD_RE.findall(_normal_title(right).casefold())
    return (
        bool(left_tokens)
        and len(left_tokens) <= len(right_tokens)
        and left_tokens == right_tokens[: len(left_tokens)]
    )


def _is_character_prefix(left: str, right: str) -> bool:
    """Also catch a source-field cut in the middle of its final word."""
    left_key, right_key = _title_key(left), _title_key(right)
    return bool(left_key) and len(left_key) < len(right_key) and right_key.startswith(left_key)


def _full_nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = SPACE_RE.sub(" ", value).strip()
    return cleaned or None


def _manual_titles(decisions: Mapping[str, Any]) -> dict[str, str]:
    values = decisions.get("decisions")
    if not isinstance(values, list):
        raise ValueError("title decisions has no decisions list")
    result: dict[str, str] = {}
    for decision in values:
        if not isinstance(decision, dict):
            raise ValueError("title decision is not an object")
        key = _full_nonempty_text(decision.get("record_key"))
        title = _full_nonempty_text(decision.get("title"))
        if key is None or title is None:
            raise ValueError("title decision lacks record_key or title")
        if key in result:
            raise ValueError(f"duplicate manual title decision: {key}")
        result[key] = _normal_title(title)
    return result


def _research_rows(batch_paths: list[Path]) -> dict[str, dict[str, Any]]:
    if len(batch_paths) != 5:
        raise ValueError("exactly five code-research batches are required")
    rows: list[dict[str, Any]] = []
    for path in sorted(batch_paths, key=lambda item: item.name.casefold()):
        for line_number, row in enumerate(_jsonl(path), start=1):
            status = _full_nonempty_text(row.get("status"))
            if status not in ALL_CODE_STATUSES:
                raise ValueError(f"unsupported research status at {path}:{line_number}: {status}")
            row = dict(row)
            row["_source_batch"] = path.name
            row["_source_line"] = line_number
            rows.append(row)
    if len(rows) != EXPECTED_COUNT:
        raise ValueError(f"code-research rows must total {EXPECTED_COUNT}, got {len(rows)}")
    result = _map_unique(rows, "paper_id", "code research")
    public = sum(row["status"] == PUBLIC_STATUS for row in rows)
    non_public = len(rows) - public
    if public != 237 or non_public != 139:
        raise ValueError(
            f"unexpected code-research totals: public={public}, non-public={non_public}"
        )
    return result


def _author_values(row: Mapping[str, Any]) -> tuple[Any, str | None]:
    authors = row.get("authors")
    if isinstance(authors, list) and authors and all(_full_nonempty_text(item) for item in authors):
        return [str(item).strip() for item in authors], None
    display = _full_nonempty_text(row.get("authors_display"))
    if display is not None:
        # A string preserves a verified flattened display without inventing person boundaries.
        return display, display
    raise ValueError(f"author resolution has no usable author value: {row.get('paper_id')}")


def _abstract_from_pdf(path: str) -> str | None:
    """Extract only a labelled first-page abstract; never synthesize one."""
    source = Path(path)
    try:
        pages = PdfReader(source).pages[:2]
        text = "\n".join((page.extract_text() or "") for page in pages)
    except Exception:
        return None
    marker = re.search(r"(?im)^\s*abstract\s*$", text)
    if marker is None:
        return None
    tail = text[marker.end() :]
    # ICLR extraction frequently emits ``1 I NTRODUCTION``; ACL may use the
    # conventional ``1 Introduction``.  Both are explicit section boundaries.
    end = re.search(
        r"(?im)^\s*(?:1\s+(?:i\s*)?n\s*t\s*r\s*o\s*d\s*u\s*c\s*t\s*i\s*o\s*n|1\.\s*introduction|introduction)\b",
        tail,
    )
    # ACL Findings papers may begin directly with a named Section 1 rather
    # than an Introduction heading.
    if end is None:
        end = re.search(r"(?m)^\s*1\s+[A-Z][A-Za-z -]{2,}$", tail)
    if end is None:
        return None
    candidate = tail[: end.start()]
    candidate = re.sub(r"(?<=[A-Za-z])[-‐]\s+(?=[a-z])", "", candidate)
    candidate = SPACE_RE.sub(" ", candidate).strip()
    # A single token, contact line, or figure caption is not an abstract.
    return candidate if len(candidate) >= 120 else None


def _adavideorag_abstract_from_text(text: str) -> str | None:
    """Recover AdaVideoRAG's explicit pre-date first-page abstract text."""
    start = "Multimodal Large Language Models (MLLMs) have demonstrated excellent performance"
    end = "establishing a new paradigm for adaptive retrieval augmentation in video analysis."
    start_at = text.find(start)
    end_at = text.find(end, start_at)
    if start_at < 0 or end_at < 0:
        return None
    candidate = text[start_at : end_at + len(end)]
    candidate = re.sub(r"(?<=[A-Za-z])[-‐]\s+(?=[a-z])", "", candidate)
    candidate = SPACE_RE.sub(" ", candidate).strip()
    return candidate if len(candidate) >= 120 else None


def _adavideorag_unlabelled_abstract(path: str) -> str | None:
    """Read and recover AdaVideoRAG's explicit pre-date first-page abstract."""
    try:
        text = PdfReader(Path(path)).pages[0].extract_text() or ""
    except Exception:
        return None
    return _adavideorag_abstract_from_text(text)


def _resolve_abstract(record: dict[str, Any]) -> dict[str, str]:
    existing = _full_nonempty_text(record.get("abstract"))
    if existing is not None:
        record["abstract"] = existing
        return {"status": "preserved_from_applied_record"}
    source_path = _full_nonempty_text(record.get("source_pdf_path"))
    recovered = _abstract_from_pdf(source_path) if source_path else None
    if recovered is not None:
        record["abstract"] = recovered
        return {
            "status": "extracted_from_labelled_pdf_abstract",
            "source_pdf_path": source_path or "",
            "evidence": (
                "First-page PDF text between the Abstract heading and Introduction heading."
            ),
        }
    if record.get("paper_id") == ADAVIDEORAG_ID and source_path:
        recovered = _adavideorag_unlabelled_abstract(source_path)
        if recovered is not None:
            record["abstract"] = recovered
            return {
                "status": "extracted_from_pdf_pre_introduction_abstract",
                "source_pdf_path": source_path,
                "evidence": (
                    "Page-one abstract delimited by its verified opening and closing sentences "
                    "before the Date and Introduction blocks."
                ),
            }
    record["abstract"] = "待核验：未能从正式 PDF 中可靠分离带标签的原文摘要；请以 PDF 正文为准。"
    warning = "abstract_pending_verification_no_reliable_labelled_pdf_extraction"
    record["warnings"] = sorted(set([*(record.get("warnings") or []), warning]))
    return {
        "status": "explicit_pending_verification",
        "source_pdf_path": source_path or "",
        "evidence": (
            "No labelled abstract could be separated safely from the local PDF text; "
            "no summary was invented."
        ),
    }


def _code_block(row: Mapping[str, Any]) -> dict[str, Any]:
    status = str(row["status"])
    urls = row.get("evidence_urls")
    if (
        not isinstance(urls, list)
        or not urls
        or not all(_full_nonempty_text(item) for item in urls)
    ):
        raise ValueError(f"code-research evidence URLs missing: {row.get('paper_id')}")
    common: dict[str, Any] = {
        "status": "official_url_only" if status == PUBLIC_STATUS else status,
        "url": row.get("official_url") if status in {PUBLIC_STATUS, "announced_pending"} else None,
        "local_path": None,
        "commit": row.get("commit") if status in {PUBLIC_STATUS, "announced_pending"} else None,
        "license": row.get("license"),
        "evidence": [
            f"code research status: {status}",
            f"reviewed at: {row.get('reviewed_at')}",
            f"source: {row.get('_source_batch')}:{row.get('_source_line')}",
            str(row.get("evidence_reason") or ""),
            *[str(item) for item in urls],
        ],
        "research_status": status,
        "exact_title_evidence": row.get("exact_title"),
    }
    if status == PUBLIC_STATUS:
        if not _full_nonempty_text(row.get("official_url")) or not _full_nonempty_text(
            row.get("commit")
        ):
            raise ValueError(f"official public result lacks URL or commit: {row.get('paper_id')}")
    return common


def _canonical_repository_url(value: Any) -> str:
    raw = _full_nonempty_text(value)
    if raw is None:
        raise ValueError("repository URL is empty")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"malformed repository URL: {raw}") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"repository URL must be clean HTTPS: {raw}")
    segments = [part for part in parsed.path.strip("/").removesuffix(".git").split("/") if part]
    host = parsed.hostname.casefold()
    # A paper can point at an official subdirectory while the local archive is
    # necessarily the repository root.  Compare repository identity, not the
    # sparse/tree presentation path (e.g. microsoft/MInference/tree/main/LeanK).
    if host == "github.com" and len(segments) >= 4 and segments[2].casefold() == "tree":
        segments = segments[:2]
    elif host == "gitlab.com" and "-" in segments:
        marker = segments.index("-")
        if len(segments) >= marker + 3 and segments[marker + 1].casefold() == "tree":
            segments = segments[:marker]
    path = "/".join(segments)
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"repository URL path is unsafe: {raw}")
    return f"https://{host}/{path.casefold()}"


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _apply_local_code_audit(
    records: list[dict[str, Any]], *, audit_path: Path, core_root: Path
) -> int:
    """Upgrade only reviewed public code to local archives, or fail closed."""
    audit = _json_object(audit_path)
    rows = audit.get("papers")
    if not isinstance(rows, list):
        raise ValueError("local code audit has no papers list")
    audit_map = _map_unique(
        [row for row in rows if isinstance(row, dict)], "paper_id", "local code audit"
    )
    record_ids = {str(record["paper_id"]) for record in records}
    if set(audit_map) != record_ids or len(audit_map) != EXPECTED_COUNT:
        missing = sorted(record_ids - set(audit_map))
        extra = sorted(set(audit_map) - record_ids)
        raise ValueError(
            f"local code audit/record ID sets differ; missing={missing[:5]}, extra={extra[:5]}"
        )
    resolved_root = core_root.expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError(f"core root is not a directory: {resolved_root}")
    updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    problems: list[str] = []
    for record in records:
        paper_id = str(record["paper_id"])
        code = record.get("code")
        if not isinstance(code, dict):
            problems.append(f"{paper_id}: code block is missing")
            continue
        if code.get("research_status") != PUBLIC_STATUS:
            continue
        audit_row = audit_map[paper_id]
        selected = audit_row.get("selected_local_archive")
        if audit_row.get("status") != "official_local" or not isinstance(selected, dict):
            problems.append(f"{paper_id}: expected official_local selected archive")
            continue
        evidence = selected.get("evidence")
        if not isinstance(evidence, dict) or selected.get("ready") is not True:
            problems.append(f"{paper_id}: selected archive is not audit-ready")
            continue
        expected_url = _canonical_repository_url(code.get("url"))
        try:
            selected_url = _canonical_repository_url(selected.get("url"))
        except ValueError as exc:
            problems.append(f"{paper_id}: invalid selected archive URL ({exc})")
            continue
        declared_commit = _full_nonempty_text(code.get("commit"))
        selected_commit = _full_nonempty_text(selected.get("expected_commit"))
        actual_commit = _full_nonempty_text(evidence.get("commit"))
        path_value = _full_nonempty_text(selected.get("path"))
        evidence_path = _full_nonempty_text(evidence.get("path"))
        if (
            expected_url != selected_url
            or not declared_commit
            or selected_commit != declared_commit
            or actual_commit != declared_commit
            or not path_value
            or not evidence_path
            or evidence.get("inside_core") is not True
            or evidence.get("dirty") is not False
        ):
            problems.append(f"{paper_id}: URL, commit, cleanliness, or core evidence mismatch")
            continue
        archive_path = Path(path_value)
        if not _inside_root(archive_path, resolved_root):
            problems.append(f"{paper_id}: selected archive escapes core root")
            continue
        try:
            if archive_path.resolve(strict=True) != Path(evidence_path).resolve(strict=True):
                problems.append(f"{paper_id}: selected archive path/evidence path differ")
                continue
        except OSError:
            problems.append(f"{paper_id}: selected archive path cannot be resolved")
            continue
        remotes = evidence.get("remotes")
        if not isinstance(remotes, list):
            problems.append(f"{paper_id}: audit evidence lacks remotes")
            continue
        remote_urls = {
            _canonical_repository_url(remote.get("url"))
            for remote in remotes
            if isinstance(remote, dict) and _full_nonempty_text(remote.get("url"))
        }
        if expected_url not in remote_urls:
            problems.append(f"{paper_id}: local remote does not match approved official URL")
            continue
        license_value = _full_nonempty_text(selected.get("effective_license"))
        if license_value is None:
            problems.append(f"{paper_id}: selected archive has no effective license")
            continue
        upgraded = dict(code)
        upgraded.update(
            {
                "status": "official_local",
                "url": selected.get("url"),
                "local_path": str(archive_path.resolve(strict=True)),
                "commit": actual_commit,
                "license": license_value,
                "evidence": [
                    *(code.get("evidence") or []),
                    "local-code-audit: official_local selected archive verified",
                    f"local-code-audit path: {archive_path.resolve(strict=True)}",
                    f"local-code-audit commit: {actual_commit}",
                    f"local-code-audit remote: {selected.get('url')}",
                ],
            }
        )
        updates.append((record, upgraded))
    if problems:
        raise ValueError("local code audit rejected: " + "; ".join(problems[:20]))
    if len(updates) != 237:
        raise ValueError(f"local code audit upgrades {len(updates)} public records, expected 237")
    for record, upgraded in updates:
        record["code"] = upgraded
        record["finalization"]["local_code_audit"] = str(audit_path)
    return len(updates)


def _append_source_url(record: dict[str, Any], *, label: str, url: str) -> None:
    values = record.setdefault("source_urls", [])
    if not isinstance(values, list):
        values = []
        record["source_urls"] = values
    if not any(isinstance(item, dict) and item.get("url") == url for item in values):
        values.append({"label": label, "url": url})


def _apply_emcompress(record: dict[str, Any], recovery: Mapping[str, Any]) -> dict[str, Any]:
    if record.get("paper_id") != EMCOMPRESS_ID or recovery.get("paper_id") != EMCOMPRESS_ID:
        raise ValueError("EMCompress recovery does not match the canonical record")
    canonical = _full_nonempty_text(recovery.get("canonical_title"))
    alternate = _full_nonempty_text(recovery.get("alternate_title"))
    local_path = _full_nonempty_text(recovery.get("local_path"))
    digest = _full_nonempty_text(recovery.get("sha256"))
    if None in {canonical, alternate, local_path, digest}:
        raise ValueError("EMCompress recovery is incomplete")
    legacy_pdf = dict(record.get("pdf") or {})
    legacy_source_path = record.get("source_pdf_path")
    legacy_target_path = record.get("target_pdf_path")
    record["title"] = canonical
    record["arxiv_id"] = _full_nonempty_text(recovery.get("arxiv_id"))
    record["title_aliases"] = [alternate]
    record["version_relation"] = "same-paper-title-and-terminology-revision"
    # The recovered 28-page arXiv file is the current EMCompress canonical
    # version.  The 26-page ``paper.pdf`` is the earlier TVS version and must
    # remain an alternate representation rather than the record's primary PDF.
    record["pdf"] = {
        "path": local_path,
        "sha256": digest,
        "size": recovery.get("bytes"),
        "page_count": recovery.get("pages"),
        "version": "arxiv-2508.21094-current-emcompress",
    }
    record["source_pdf_path"] = local_path
    record["target_pdf_path"] = local_path
    record["pdf_representations"] = [
        {
            "role": "canonical_current",
            "title": canonical,
            "path": local_path,
            "sha256": digest,
            "size": recovery.get("bytes"),
            "page_count": recovery.get("pages"),
            "source_url": recovery.get("source_url"),
        },
        {
            "role": "alternate_legacy_tvs",
            "title": alternate,
            "path": legacy_target_path,
            "source_pdf_path": legacy_source_path,
            "sha256": legacy_pdf.get("sha256"),
            "size": legacy_pdf.get("size"),
            "page_count": legacy_pdf.get("page_count"),
        },
    ]
    _append_source_url(record, label="arXiv", url=str(recovery.get("source_url")))
    record["decision_evidence"] = [
        *(record.get("decision_evidence") or []),
        *[str(item) for item in recovery.get("evidence", [])],
    ]
    record["warnings"] = [
        warning
        for warning in (record.get("warnings") or [])
        if "do not infer or attach EMCompress code" not in str(warning)
        and not str(warning).startswith("canonical_title_mismatch_resolved_by_qa")
    ]
    return {
        "rule": "emcompress_version_recovery",
        "old_title": None,
        "new_title": canonical,
        "evidence": [str(item) for item in recovery.get("evidence", [])],
    }


def _title_for_record(
    record: Mapping[str, Any], research: Mapping[str, Any], manual_titles: Mapping[str, str]
) -> tuple[str, str]:
    current = _normal_title(record.get("title"))
    agent = _normal_title(research.get("exact_title"))
    key = _full_nonempty_text(record.get("title_qa", {}).get("record_key"))
    manual = manual_titles.get(key or "")
    paper_id = str(record["paper_id"])
    if paper_id in PRIMARY_PDF_TITLE_IDS:
        return current, "primary_pdf_title"
    if paper_id in AGENT_TITLE_IDS:
        return agent, "agent_title_primary_evidence"
    if manual is not None:
        return manual, "manual_title_decision"
    if _title_key(agent) == _title_key(current):
        return agent, "agent_normalized_exact_smallcaps_cleanup"
    if _is_token_prefix(agent, current) or _is_character_prefix(agent, current):
        return current, "longer_complete_prefix_from_record"
    if _is_token_prefix(current, agent) or _is_character_prefix(current, agent):
        return agent, "longer_complete_prefix_from_agent"
    raise ValueError(
        f"unresolved non-prefix title conflict for {paper_id}: record={current!r}; agent={agent!r}"
    )


def _finalize_records(
    records: list[dict[str, Any]],
    authors: Mapping[str, dict[str, Any]],
    research: Mapping[str, dict[str, Any]],
    manual_titles: Mapping[str, str],
    recovery: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    record_map = _map_unique(records, "paper_id", "applied paper records")
    expected = set(record_map)
    for label, values in (("author resolution", authors), ("code research", research)):
        if set(values) != expected:
            missing, extra = sorted(expected - set(values)), sorted(set(values) - expected)
            raise ValueError(
                f"{label}/record ID sets differ; missing={missing[:5]}, extra={extra[:5]}"
            )
    if len(expected) != EXPECTED_COUNT:
        raise ValueError(f"records must contain exactly {EXPECTED_COUNT} unique IDs")
    if recovery.get("paper_id") not in expected:
        raise ValueError("EMCompress recovery paper is absent from applied records")

    changes: list[dict[str, Any]] = []
    abstract_counts: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    for paper_id in sorted(expected):
        source = record_map[paper_id]
        record = json.loads(json.dumps(source, ensure_ascii=False))
        old_title = _normal_title(record.get("title"))
        if paper_id == EMCOMPRESS_ID:
            title_change = _apply_emcompress(record, recovery)
            new_title = _normal_title(record["title"])
            rule = title_change["rule"]
        else:
            new_title, rule = _title_for_record(record, research[paper_id], manual_titles)
            record["title"] = new_title
        if old_title != new_title:
            changes.append(
                {
                    "paper_id": paper_id,
                    "old_title": old_title,
                    "new_title": new_title,
                    "rule": rule,
                    "agent_exact_title": _normal_title(research[paper_id].get("exact_title")),
                    "manual_title": manual_titles.get(
                        _full_nonempty_text(record.get("title_qa", {}).get("record_key")) or ""
                    ),
                }
            )

        author_value, display = _author_values(authors[paper_id])
        record["authors"] = author_value
        record["authors_display"] = display
        record["author_resolution"] = authors[paper_id]
        record["warnings"] = [
            warning
            for warning in (record.get("warnings") or [])
            if str(warning) != "canonical authors are missing and require evidence review"
            and not str(warning).startswith("authors_not_overridden_from_low_confidence")
        ]
        record["code"] = _code_block(research[paper_id])
        abstract_resolution = _resolve_abstract(record)
        abstract_counts[abstract_resolution["status"]] += 1
        record["abstract_resolution"] = abstract_resolution
        record["schema_version"] = SCHEMA_VERSION
        record["finalization"] = {
            "title_rule": rule,
            "code_research_batch": research[paper_id]["_source_batch"],
            "code_research_line": research[paper_id]["_source_line"],
        }
        for field in ("title", "authors", "year", "venue", "abstract"):
            value = record.get(field)
            if value is None or value == "" or value == []:
                raise ValueError(f"final record has empty {field}: {paper_id}")
        if SMALLCAPS_SPACED_RE.search(str(record["title"])):
            raise ValueError(f"unresolved small-caps title spacing: {paper_id}: {record['title']}")
        if TRUNCATED_TITLE_END_RE.search(str(record["title"])):
            raise ValueError(f"title has a truncation-like ending: {paper_id}: {record['title']}")
        result.append(record)
    return result, changes, abstract_counts


def _csv_rows(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        code = record.get("code") or {}
        rows.append(
            {
                "paper_id": record["paper_id"],
                "title": record["title"],
                "authors": "; ".join(record["authors"])
                if isinstance(record["authors"], list)
                else record["authors"],
                "year": record["year"],
                "venue": record["venue"],
                "abstract_status": (record.get("abstract_resolution") or {}).get("status"),
                "code_status": code.get("status"),
                "code_url": code.get("url"),
                "code_commit": code.get("commit"),
                "domain_slugs": "; ".join(record.get("domain_slugs") or []),
                "pdf_sha256": (record.get("pdf") or {}).get("sha256"),
            }
        )
    return rows


def _write_outputs(
    prefix: Path,
    records: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    abstract_counts: Counter[str],
    *,
    replace_output: bool,
    local_code_audit_applied: bool,
) -> dict[str, Path]:
    outputs = {
        "json": prefix.with_suffix(".json"),
        "jsonl": prefix.with_suffix(".jsonl"),
        "csv": prefix.with_suffix(".csv"),
        "markdown": prefix.with_suffix(".md"),
        "title_changes": prefix.with_name(prefix.name + "-title-changes").with_suffix(".json"),
    }
    existing = [path for path in outputs.values() if path.exists()]
    if existing and not replace_output:
        raise FileExistsError(
            "refusing to overwrite finalization output: " + ", ".join(map(str, existing))
        )
    prefix.parent.mkdir(parents=True, exist_ok=True)
    status_counts = Counter(str((record.get("code") or {}).get("status")) for record in records)
    payload = {
        "schema_version": (
            LOCAL_CODE_AUDIT_SCHEMA_VERSION if local_code_audit_applied else SCHEMA_VERSION
        ),
        "record_count": len(records),
        "paper_ids": [record["paper_id"] for record in records],
        "code_status_counts": dict(sorted(status_counts.items())),
        "abstract_resolution_counts": dict(sorted(abstract_counts.items())),
        "title_change_count": len(changes),
        "records": records,
    }
    outputs["json"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    outputs["jsonl"].write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
        ),
        encoding="utf-8",
    )
    csv_rows = _csv_rows(records)
    with outputs["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    outputs["title_changes"].write_text(
        json.dumps(
            {
                "schema_version": "paper-title-change-audit/v1",
                "change_count": len(changes),
                "changes": changes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown = [
        "# 2026-08-31 最终 paper-record 元数据收口",
        "",
        f"- final records: {len(records)} "
        f"(unique IDs: {len({record['paper_id'] for record in records})})",
        "- code: "
        + ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items())),
        "- abstract resolution: "
        + ", ".join(f"{status}={count}" for status, count in sorted(abstract_counts.items())),
        f"- title changes: {len(changes)}; audit: `{outputs['title_changes'].name}`",
        "- empty title/authors/year/venue/abstract: 0",
        "- unresolved small-caps title-spacing / title-prefix truncation: 0",
        "",
        (
            "All 237 approved public repositories were independently confirmed as clean, fixed "
            "local official archives by the supplied paper-code audit."
            if local_code_audit_applied
            else "`official_url_only` means official public code evidence is approved but local "
            "archival is intentionally not claimed by this derived metadata artifact."
        ),
        "`none_found`, `third_party_only`, and `announced_pending` retain their reviewed "
        "evidence rather than being silently treated as missing.",
        "",
        "EMCompress remains one canonical paper record and includes two explicit PDF "
        "representations: current canonical EMCompress and the recovered Temporal Visual "
        "Screening version.",
    ]
    outputs["markdown"].write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return outputs


def finalize(
    *,
    records_path: Path,
    author_resolution_path: Path,
    title_decisions_path: Path,
    recovery_path: Path,
    batch_paths: list[Path],
    output_prefix: Path,
    replace_output: bool = False,
    local_code_audit_path: Path | None = None,
    core_root: Path | None = None,
) -> tuple[dict[str, Path], dict[str, Any]]:
    records = _jsonl(records_path)
    author_map = _map_unique(_jsonl(author_resolution_path), "paper_id", "author resolution")
    research = _research_rows(batch_paths)
    manual = _manual_titles(_json_object(title_decisions_path))
    finalized, changes, abstract_counts = _finalize_records(
        records, author_map, research, manual, _json_object(recovery_path)
    )
    if (local_code_audit_path is None) != (core_root is None):
        raise ValueError("--local-code-audit and --core-root must be provided together")
    local_upgrades = 0
    if local_code_audit_path is not None and core_root is not None:
        local_upgrades = _apply_local_code_audit(
            finalized, audit_path=local_code_audit_path, core_root=core_root
        )
        for record in finalized:
            record["schema_version"] = LOCAL_CODE_AUDIT_SCHEMA_VERSION
    outputs = _write_outputs(
        output_prefix,
        finalized,
        changes,
        abstract_counts,
        replace_output=replace_output,
        local_code_audit_applied=local_upgrades > 0,
    )
    return outputs, {
        "record_count": len(finalized),
        "code_status_counts": Counter(
            (record.get("code") or {}).get("status") for record in finalized
        ),
        "abstract_resolution_counts": abstract_counts,
        "title_change_count": len(changes),
        "local_code_audit_upgrades": local_upgrades,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--author-resolution", type=Path, required=True)
    parser.add_argument("--title-decisions", type=Path, required=True)
    parser.add_argument("--emcompress-recovery", type=Path, required=True)
    parser.add_argument("--batch", type=Path, action="append", required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument(
        "--local-code-audit",
        type=Path,
        help="Full 376-row paper_code_audit JSON; upgrades public code or fails closed.",
    )
    parser.add_argument(
        "--core-root",
        type=Path,
        help="Required with --local-code-audit to enforce archive placement inside core.",
    )
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help="Intentionally rebuild this tool's own derived output set.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        outputs, summary = finalize(
            records_path=args.records,
            author_resolution_path=args.author_resolution,
            title_decisions_path=args.title_decisions,
            recovery_path=args.emcompress_recovery,
            batch_paths=args.batch,
            output_prefix=args.output_prefix,
            replace_output=args.replace_output,
            local_code_audit_path=args.local_code_audit,
            core_root=args.core_root,
        )
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"paper-record finalization failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"outputs": {key: str(value) for key, value in outputs.items()}, **summary},
            ensure_ascii=False,
            default=dict,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
