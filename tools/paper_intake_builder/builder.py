"""Normalize the 376-paper acquisition batch and build a safe PDF copy plan.

The builder is deliberately separate from Research Asset Hub ingestion.  It reads the
metadata extractor output, the Video recovery evidence map, and the existing VideoLLM
catalog.  It emits auditable paper records and copy actions but never edits a registered
source, Hub configuration, or an existing domain unit.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from pypdf import PdfReader

SCHEMA_VERSION = "paper-intake-builder/v1"
IDENTITY_DECISIONS_SCHEMA_VERSION = "paper-intake-identity-decisions/v1"
ROUTING_DECISIONS_SCHEMA_VERSION = "paper-intake-routing-decisions/v1"
TITLE_DECISIONS_SCHEMA_VERSION = "paper-title-review-decisions/v1"
BATCH_GROUP_COUNTS = {
    "video-main-current": 55,
    "video-main-recovered": 40,
    "embodied-main": 44,
    "navigation-main": 20,
    "video-deleted-review": 17,
    "embodied-excluded-review": 9,
    "other-review": 191,
}
COMPRESS_UNIT_DIRS = {
    "video-llm-compression": "VLM框架3DV压缩",
    "embodied-ai-compression": "具身智能压缩",
    "navigation-compression": "导航压缩",
}
PERSONAL_INTAKE_DIR = Path("个人新增资料") / "2026-08-31-顶会压缩论文"
DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"

ARXIV_RE = re.compile(
    r"(?i)(?<![0-9])(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)?"
    r"(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?![0-9])"
)
DOI_RE = re.compile(r"(?i)(?<![A-Z0-9])10\.\d{4,9}/[-._;()/:A-Z0-9]+")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class FileInfo:
    sha256: str
    size: int
    page_count: int


@dataclass
class BuildResult:
    report: dict[str, Any]
    records: list[dict[str, Any]]
    copy_actions: list[dict[str, Any]]

    @property
    def ready(self) -> bool:
        return self.report.get("status") == "ready"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", " ").split()).strip()
    return text or None


def _clean_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [part.strip() for part in value.split(";") if part.strip()]
        value = decoded
    if not isinstance(value, list):
        value = [value]
    result: list[str] = []
    for item in value:
        cleaned = _clean_text(item)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(path: Path) -> FileInfo:
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError(f"not a PDF (missing %PDF- magic): {path}")
    digest = sha256_file(path)
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # pypdf exposes backend-specific errors
            raise ValueError(f"encrypted PDF cannot be opened: {path}: {exc}") from exc
    pages = len(reader.pages)
    if pages < 1:
        raise ValueError(f"PDF has no pages: {path}")
    return FileInfo(sha256=digest, size=path.stat().st_size, page_count=pages)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL row {line_number} is not an object")
            source = row.get("source") if isinstance(row.get("source"), dict) else {}
            raw_path = source.get("path") or row.get("source_path")
            if not raw_path:
                raise ValueError(f"JSONL row {line_number} has no source path")
            latest[os.path.normcase(str(raw_path))] = row
    return list(latest.values())


def _csv_to_metadata(row: dict[str, str]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for field in ("title", "year", "venue", "abstract"):
        value: Any = row.get(field)
        if field == "year" and value:
            try:
                value = int(value)
            except ValueError:
                pass
        if value not in (None, ""):
            facts[field] = {"value": value}
    authors = _json_value(row.get("authors_json"), [])
    if authors:
        facts["authors"] = {"value": authors}
    for field, column in (("arxiv_id", "arxiv_ids_json"), ("doi", "dois_json")):
        values = _json_value(row.get(column), [])
        if values:
            if not isinstance(values, list):
                values = [values]
            facts[field] = [{"value": item} for item in values]
    parser: dict[str, Any] = {}
    if row.get("page_count"):
        parser["page_count"] = int(row["page_count"])
    return {
        "schema_version": "paper-metadata/csv-v1",
        "status": row.get("status") or "ok",
        "source": {
            "path": row.get("source_path"),
            "sha256": row.get("sha256"),
            "size": int(row["size"]) if row.get("size") else None,
        },
        "parser": parser,
        "facts": facts,
        "warnings": _json_value(row.get("warnings_json"), []),
        "error": _json_value(row.get("error_json"), None),
    }


def load_metadata(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".jsonl":
        return _read_jsonl(path)
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [_csv_to_metadata(row) for row in csv.DictReader(handle)]
    raise ValueError("metadata input must be .jsonl or .csv")


def _fact_raw(record: dict[str, Any], field: str) -> Any:
    facts = record.get("facts") if isinstance(record.get("facts"), dict) else {}
    fact = facts.get(field)
    if isinstance(fact, dict):
        return fact.get("value")
    if isinstance(fact, list):
        return [item.get("value") for item in fact if isinstance(item, dict)]
    return None


def _normalize_arxiv(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    match = ARXIV_RE.search(text)
    return match.group("id") if match else None


def _normalize_doi(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    match = DOI_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;)]}").casefold()


def _single_identifier(record: dict[str, Any], field: str) -> tuple[str | None, list[str]]:
    raw = _fact_raw(record, field)
    values = raw if isinstance(raw, list) else [raw]
    normalizer = _normalize_arxiv if field == "arxiv_id" else _normalize_doi
    normalized = sorted({value for item in values if (value := normalizer(item))})
    if len(normalized) == 1:
        return normalized[0], []
    if len(normalized) > 1:
        return None, [f"conflicting_{field}_facts:{','.join(normalized)}"]
    return None, []


def _strip_read_marker(stem: str) -> str:
    return re.sub(
        r"(?i)^\s*(?:\[(?:read|unread)\]|(?:read|unread|\u5df2\u8bfb|\u672a\u8bfb))[\s_-]+",
        "",
        stem,
    )


def filename_fields(path: Path) -> dict[str, str | int | None]:
    stem = _strip_read_marker(path.stem.strip())
    match = re.match(r"^(?P<venue>.+?)\s+(?P<year>(?:19|20)\d{2})\s+(?P<title>.+)$", stem)
    if match:
        venue = _clean_text(match.group("venue"))
        year = int(match.group("year"))
        tail = match.group("title").strip()
    else:
        venue = None
        year_match = YEAR_RE.search(stem)
        year = int(year_match.group(1)) if year_match else None
        tail = stem
    # Batch filenames use ``_ `` as the method/title delimiter.  A bare underscore may
    # be part of LaTeX in a method name (for example ``$_text{S}^2$Q-VDiT``), so it
    # must not split the identity stem.
    parts = re.split(r"_(?=\s)|[：:]", tail, maxsplit=1)
    method = _clean_text(parts[0])
    title = _clean_text(": ".join(part.strip() for part in parts if part.strip()))
    return {"venue": venue, "year": year, "method": method, "title": title}


def _slugify(value: str, *, max_length: int = 56) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return slug[:max_length].rstrip("-")


def _method_slug(title: str | None, filename_method: str | None = None) -> str | None:
    candidate = _clean_text(filename_method)
    if not candidate and title:
        candidate = re.split(r"[:：]", title, maxsplit=1)[0]
    if not candidate:
        return None
    words = candidate.split()
    if len(words) > 8:
        candidate = " ".join(words[:8])
    return _slugify(candidate) or None


def stable_paper_id(
    *, arxiv_id: str | None, title: str | None, year: int | None, filename_method: str | None
) -> str:
    normalized_arxiv = _normalize_arxiv(arxiv_id)
    if normalized_arxiv:
        return f"arxiv-{normalized_arxiv.replace('.', '-')}"
    method = _method_slug(title, filename_method)
    if not method or year is None:
        raise ValueError("paper ID requires an arXiv ID or a method name plus year")
    return f"{method}-{year}"


def _normalized_title(title: str | None) -> str:
    if not title:
        return ""
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def source_group_for(path: Path, desktop_root: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(desktop_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"metadata PDF is outside the Desktop acquisition root: {path}") from exc
    parts = [part.casefold() for part in relative.parts]
    if not parts:
        raise ValueError(f"cannot classify Desktop root itself: {path}")
    if parts[0] == "video":
        if len(parts) >= 3 and parts[1:3] == ["work", "deleted_pdf"]:
            return "video-deleted-review"
        if len(parts) == 2:
            return "video-main-current"
    if parts[0] == "embodied":
        if len(parts) >= 2 and parts[1] == "排除_非压缩或部分相关".casefold():
            return "embodied-excluded-review"
        if len(parts) == 2:
            return "embodied-main"
    if parts[0] == "vln" and len(parts) == 2:
        return "navigation-main"
    if parts[0] == "其他".casefold() and len(parts) == 2:
        return "other-review"
    raise ValueError(f"PDF path does not match an audited acquisition group: {relative}")


SPECIFIC_ROUTE_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "route-image-editing",
        "image-editing",
        ("image editing", "instruction guided editing", "text guided image edit"),
    ),
    (
        "route-wsvad",
        "wsvad",
        ("video anomaly", "anomaly detection", "weakly supervised anomaly"),
    ),
    (
        "route-game-rl",
        "game-rl",
        ("game playing", "game agent", "gameplay", "video game"),
    ),
    (
        "route-navigation",
        "vision-language-navigation",
        (
            "vision language navigation",
            "vln",
            "objectnav",
            "object goal navigation",
            "embodied navigation",
        ),
    ),
    (
        "route-generation",
        "generation",
        (
            "video generation",
            "image generation",
            "diffusion",
            "flow matching",
            "diffusion transformer",
            "latent video",
            "video vae",
        ),
    ),
    (
        "route-world-model",
        "world-model",
        ("world model", "world-model"),
    ),
    (
        "route-fine-tuning",
        "fine-tuning",
        ("fine tuning", "finetuning", "lora", "parameter efficient", "peft"),
    ),
    (
        "route-reinforcement-learning",
        "reinforcement-learning",
        ("reinforcement learning", "policy optimization", "reward model", "rlhf"),
    ),
    (
        "route-agent",
        "agent",
        (
            "agent memory",
            "tool use",
            "tool-use",
            "gui agent",
            "web agent",
            "retrieval augmented",
            "rag ",
        ),
    ),
    (
        "route-llm-tech-report",
        "llm-tech-reports",
        ("technical report", "model card"),
    ),
)
INFRA_TERMS = (
    "compression",
    "quantization",
    "pruning",
    "kv cache",
    "token merging",
    "speculative decoding",
    "inference acceleration",
    "efficient inference",
    "context reduction",
    "sparse attention",
)


def route_domain(source_group: str, title: str, filename: str) -> dict[str, Any]:
    if source_group in {"video-main-current", "video-main-recovered"}:
        return {
            "domain": "video-llm-compression",
            "rule_ids": ["source-group-video-keep-95"],
            "reason": "Video keep-list main set (55 present + 40 recovered).",
            "confidence": "source-declared",
        }
    if source_group == "embodied-main":
        return {
            "domain": "embodied-ai-compression",
            "rule_ids": ["source-group-embodied-main-44"],
            "reason": "Embodied main set declared as embodied-AI compression.",
            "confidence": "source-declared",
        }
    if source_group == "navigation-main":
        return {
            "domain": "navigation-compression",
            "rule_ids": ["source-group-vln-main-20"],
            "reason": "VLN main set declared as navigation compression.",
            "confidence": "source-declared",
        }

    text = unicodedata.normalize("NFKC", f"{title} {filename}").casefold()
    text = re.sub(r"[-_/]+", " ", text)
    matches: list[tuple[str, str]] = []
    for rule_id, domain, terms in SPECIFIC_ROUTE_RULES:
        if any(term in text for term in terms):
            matches.append((rule_id, domain))
    domains = sorted({domain for _, domain in matches})
    if len(domains) == 1:
        return {
            "domain": domains[0],
            "rule_ids": [rule for rule, _ in matches],
            "reason": "One specific existing-domain rule matched the title/filename.",
            "confidence": "keyword-reviewable",
        }
    if len(domains) > 1:
        return {
            "domain": "needs-review",
            "rule_ids": [rule for rule, _ in matches],
            "reason": f"Conflicting specific-domain rules matched: {', '.join(domains)}.",
            "confidence": "needs-review",
        }
    if any(term in text for term in INFRA_TERMS):
        return {
            "domain": "infra",
            "rule_ids": ["route-infra-compression-fallback"],
            "reason": (
                "No task-specific domain matched; an audited systems/compression term matched."
            ),
            "confidence": "keyword-reviewable",
        }
    return {
        "domain": "needs-review",
        "rule_ids": ["route-no-audited-rule"],
        "reason": "No unambiguous audited domain rule matched; retain for manual review.",
        "confidence": "needs-review",
    }


def _recovery_report_default(recovery_map: Path) -> Path | None:
    candidate = recovery_map.with_name(recovery_map.name.replace("-recovery-map", "-download"))
    return candidate if candidate.exists() else None


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _stable_recovery_filename(record: dict[str, Any]) -> str:
    landing = str(record.get("official_landing_url") or "")
    arxiv = _normalize_arxiv(record.get("arxiv_id"))
    if arxiv:
        identifier = arxiv
    elif "openreview.net" in landing:
        identifier = dict(
            part.split("=", 1)
            for part in urlsplit(landing).query.split("&")
            if "=" in part
        ).get("id", "openreview")
    else:
        path_parts = [part for part in urlsplit(landing).path.split("/") if part]
        identifier = path_parts[-1] if path_parts else "paper"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", identifier).strip("-") or "paper"
    return f"missing-{int(record['index']):02d}-{safe}.pdf"


def _recovery_sources(
    recovery_map: Path,
    recovery_report: Path | None,
    recovered_root: Path | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    mapping = _load_json_object(recovery_map)
    records = mapping.get("records")
    if not isinstance(records, list):
        raise ValueError("recovery map has no records array")
    by_index: dict[int, dict[str, Any]] = {}
    report_path = recovery_report or _recovery_report_default(recovery_map)
    if report_path:
        report = _load_json_object(report_path)
        report_rows = report.get("records")
        if not isinstance(report_rows, list):
            raise ValueError("recovery report has no records array")
        by_index = {
            int(row["index"]): row
            for row in report_rows
            if isinstance(row, dict) and row.get("index") is not None
        }
        if recovered_root is None and report.get("output_dir"):
            recovered_root = Path(str(report["output_dir"]))
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in records:
        if not isinstance(item, dict) or item.get("index") is None:
            raise ValueError("malformed recovery-map record")
        index = int(item["index"])
        evidence = dict(by_index.get(index, {}))
        if not evidence.get("path") and recovered_root:
            evidence["path"] = str(recovered_root / _stable_recovery_filename(item))
        result.append((item, evidence))
    return result


def _existing_vlm_entries(catalog_path: Path) -> tuple[Path, list[dict[str, Any]]]:
    payload = _load_json_object(catalog_path)
    rows = payload.get("papers")
    if not isinstance(rows, list):
        raise ValueError("VLM catalog has no papers array")
    unit_root = catalog_path.resolve().parents[2]
    entries: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pdf_status = row.get("pdf_status") if isinstance(row.get("pdf_status"), dict) else {}
        arxiv_values = [
            _normalize_arxiv(row.get("arxiv_id")),
            _normalize_arxiv(row.get("page")),
            _normalize_arxiv(row.get("pdf")),
        ]
        doi_values = [_normalize_doi(row.get("doi")), _normalize_doi(row.get("page"))]
        pdf_local = row.get("pdf_local") or pdf_status.get("path")
        entries.append(
            {
                "id": row.get("id"),
                "title": _clean_text(row.get("title")),
                "year": int(row["year"]) if row.get("year") else None,
                "arxiv_id": next((value for value in arxiv_values if value), None),
                "doi": next((value for value in doi_values if value), None),
                "sha256": _clean_text(pdf_status.get("sha256")),
                "pdf_path": str((unit_root / str(pdf_local)).resolve()) if pdf_local else None,
            }
        )
    return unit_root, entries


def _vlm_matches(record: dict[str, Any], entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    title_key = _normalized_title(record.get("title"))
    method = record.get("identity", {}).get("method_slug")
    for entry in entries:
        reasons: list[str] = []
        exact_hash = bool(record["pdf"]["sha256"] and record["pdf"]["sha256"] == entry["sha256"])
        strong = False
        if record.get("arxiv_id") and record["arxiv_id"] == entry.get("arxiv_id"):
            strong = True
            reasons.append("same_arxiv")
        if record.get("doi") and record["doi"] == entry.get("doi"):
            strong = True
            reasons.append("same_doi")
        same_title_year = bool(
            title_key
            and title_key == _normalized_title(entry.get("title"))
            and record.get("year") == entry.get("year")
        )
        same_method_year = bool(
            method
            and method == _method_slug(entry.get("title"))
            and record.get("year") == entry.get("year")
        )
        if exact_hash:
            reasons.append("same_sha256")
        if same_title_year:
            reasons.append("same_normalized_title_year")
        elif same_method_year:
            reasons.append("same_method_year")
        if not (exact_hash or strong or same_title_year or same_method_year):
            continue
        if exact_hash:
            classification = "duplicate_exact_hash"
        elif strong:
            classification = "version_candidate_strong_identity"
        elif same_title_year:
            classification = "version_candidate_title_year"
        else:
            classification = "identity_candidate_method_year"
        matches.append(
            {
                "classification": classification,
                "reasons": reasons,
                "existing_id": entry.get("id"),
                "existing_title": entry.get("title"),
                "existing_year": entry.get("year"),
                "existing_arxiv_id": entry.get("arxiv_id"),
                "existing_doi": entry.get("doi"),
                "existing_sha256": entry.get("sha256"),
                "existing_pdf_path": entry.get("pdf_path"),
            }
        )
    return matches


def _targets(core_root: Path, domain: str, paper_id: str) -> dict[str, str]:
    if domain in COMPRESS_UNIT_DIRS:
        unit = core_root / COMPRESS_UNIT_DIRS[domain]
        pdf_target = unit / "library" / "papers" / paper_id / "paper.pdf"
        html_target = unit / "library" / "site" / "papers" / f"{paper_id}.html"
        metadata_target = unit / "library" / "papers" / paper_id / "metadata.json"
        html_pdf_link = f"../../papers/{paper_id}/paper.pdf"
    else:
        unit = core_root / PERSONAL_INTAKE_DIR / domain / paper_id
        pdf_target = unit / "paper.pdf"
        html_target = unit / "paper.html"
        metadata_target = unit / "metadata.json"
        html_pdf_link = "paper.pdf"
    return {
        "pdf": str(pdf_target.resolve(strict=False)),
        "html": str(html_target.resolve(strict=False)),
        "metadata": str(metadata_target.resolve(strict=False)),
        "html_pdf_link": html_pdf_link,
    }


def _relative_file_link(target: Path, html_path: Path) -> str:
    return os.path.relpath(target, start=html_path.parent).replace("\\", "/")


def _nonempty_evidence(value: Any, *, context: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context} evidence must be a non-empty string array")
    evidence: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{context} evidence entries must be non-empty strings")
        evidence.append(item)
    return evidence


def load_identity_decisions(path: Path) -> list[dict[str, Any]]:
    payload = _load_json_object(path)
    allowed_top = {"schema_version", "decisions"}
    extra_top = sorted(set(payload) - allowed_top)
    if extra_top:
        raise ValueError(f"identity decisions contain unknown top-level fields: {extra_top}")
    if payload.get("schema_version") != IDENTITY_DECISIONS_SCHEMA_VERSION:
        raise ValueError(
            "identity decisions schema_version must be "
            f"{IDENTITY_DECISIONS_SCHEMA_VERSION!r}"
        )
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ValueError("identity decisions must contain a decisions array")
    allowed_fields = {
        "source_sha256",
        "existing_vlm_id",
        "decision",
        "version_label",
        "target_relative_path",
        "evidence",
    }
    required_fields = allowed_fields - {"evidence"}
    decisions: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"identity decision #{index} is not an object")
        missing = sorted(required_fields - set(raw))
        extra = sorted(set(raw) - allowed_fields)
        if missing or extra:
            raise ValueError(
                f"identity decision #{index} fields invalid: missing={missing}, extra={extra}"
            )
        row: dict[str, Any] = {
            key: str(raw[key]).strip() for key in required_fields
        }
        row["evidence"] = (
            _nonempty_evidence(raw["evidence"], context=f"identity decision #{index}")
            if "evidence" in raw
            else []
        )
        row["source_sha256"] = row["source_sha256"].casefold()
        if not SHA256_RE.fullmatch(row["source_sha256"]):
            raise ValueError(f"identity decision #{index} has invalid source_sha256")
        if not row["existing_vlm_id"]:
            raise ValueError(f"identity decision #{index} has empty existing_vlm_id")
        if row["decision"] != "same_identity_alternate_version":
            raise ValueError(
                f"identity decision #{index} has unsupported decision: {row['decision']}"
            )
        if not VERSION_LABEL_RE.fullmatch(row["version_label"]):
            raise ValueError(f"identity decision #{index} has invalid version_label")
        relative = row["target_relative_path"]
        if "\\" in relative:
            raise ValueError(
                f"identity decision #{index} target_relative_path must use forward slashes"
            )
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative_path.suffix.casefold() != ".pdf"
        ):
            raise ValueError(
                f"identity decision #{index} has unsafe target_relative_path: {relative}"
            )
        key = (row["source_sha256"], row["existing_vlm_id"])
        if key in seen_keys:
            raise ValueError(
                "duplicate identity decision key: "
                f"source_sha256={key[0]}, existing_vlm_id={key[1]}"
            )
        seen_keys.add(key)
        decisions.append(row)
    return decisions


def apply_identity_decisions(
    records: Sequence[dict[str, Any]],
    actions: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    *,
    vlm_unit_root: Path,
) -> tuple[list[str], int]:
    errors: list[str] = []
    applied = 0
    records_by_source = {
        os.path.normcase(str(record["source_pdf_path"])): record for record in records
    }
    actions_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for action in actions:
        source_sha = str(action.get("source_sha256") or "").casefold()
        record = records_by_source.get(os.path.normcase(str(action["source_path"])))
        if not record:
            errors.append(f"identity_decision_internal_record_missing:{action['source_path']}")
            continue
        for match in record.get("existing_vlm_matches", []):
            existing_id = str(match.get("existing_id") or "")
            if existing_id:
                actions_by_key.setdefault((source_sha, existing_id), []).append(action)

    for decision in decisions:
        key = (decision["source_sha256"], decision["existing_vlm_id"])
        matched_actions = actions_by_key.get(key, [])
        if len(matched_actions) != 1:
            errors.append(
                "identity_decision_match_count:"
                f"sha256={key[0]}:existing_vlm_id={key[1]}:actual={len(matched_actions)}"
            )
            continue
        action = matched_actions[0]
        if action["planned_action"] != "hold_identity_review":
            errors.append(
                "identity_decision_not_for_hold:"
                f"sha256={key[0]}:existing_vlm_id={key[1]}:"
                f"action={action['planned_action']}"
            )
            continue
        record = records_by_source[os.path.normcase(str(action["source_path"]))]
        selected_matches = [
            match
            for match in record.get("existing_vlm_matches", [])
            if match.get("existing_id") == decision["existing_vlm_id"]
        ]
        if len(selected_matches) != 1:
            errors.append(
                "identity_decision_existing_match_count:"
                f"sha256={key[0]}:existing_vlm_id={key[1]}:actual={len(selected_matches)}"
            )
            continue
        existing_pdf = selected_matches[0].get("existing_pdf_path")
        if not existing_pdf:
            errors.append(
                f"identity_decision_existing_pdf_missing:{decision['existing_vlm_id']}"
            )
            continue
        existing_paper_dir = Path(str(existing_pdf)).resolve(strict=False).parent
        versions_dir = (existing_paper_dir / "versions").resolve(strict=False)
        relative = PurePosixPath(decision["target_relative_path"])
        target = (vlm_unit_root / Path(*relative.parts)).resolve(strict=False)
        if target == versions_dir or not _is_within(target, versions_dir):
            errors.append(
                "identity_decision_target_outside_versions:"
                f"existing_vlm_id={decision['existing_vlm_id']}:"
                f"target={decision['target_relative_path']}:"
                f"required_root={versions_dir}"
            )
            continue

        action.update(
            {
                "planned_action": "copy_alternate_version",
                "target_path": str(target),
                "reason": (
                    "Explicit identity decision approved this source as an alternate "
                    "version of the selected existing VLM paper."
                ),
                "identity_decision": dict(decision),
                "existing_vlm_id": decision["existing_vlm_id"],
                "existing_versions_dir": str(versions_dir),
                "version_label": decision["version_label"],
                "target_relative_path": decision["target_relative_path"],
            }
        )
        record["canonical_paper_id"] = decision["existing_vlm_id"]
        record["existing_vlm_id"] = decision["existing_vlm_id"]
        record["target_pdf_path"] = str(target)
        record["pdf"]["path"] = _relative_file_link(
            target, Path(str(record["target_html_path"]))
        )
        record["pdf"]["version"] = decision["version_label"]
        record["version_relation"] = {
            "relation_type": "alternate_version_of",
            "canonical_paper_id": decision["existing_vlm_id"],
            "existing_vlm_id": decision["existing_vlm_id"],
            "version_label": decision["version_label"],
            "target_relative_path": decision["target_relative_path"],
            "decision": decision["decision"],
            "evidence": list(decision["evidence"]),
            "decision_key": {
                "source_sha256": decision["source_sha256"],
                "existing_vlm_id": decision["existing_vlm_id"],
            },
        }
        record["decision_evidence"].append(
            "explicit identity decision: same_identity_alternate_version "
            f"-> {decision['existing_vlm_id']} ({decision['version_label']})"
        )
        record["decision_evidence"].extend(decision["evidence"])
        applied += 1
    return errors, applied


def load_taxonomy_domains(path: Path) -> set[str]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("domains"), list):
        raise ValueError(f"taxonomy has no domains array: {path}")
    domains = {
        str(item["id"])
        for item in payload["domains"]
        if isinstance(item, dict) and item.get("id")
    }
    if not domains:
        raise ValueError(f"taxonomy has no domain IDs: {path}")
    return domains


def load_title_qa_results(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid title QA JSONL at line {line_number}: {exc}") from exc
            if not isinstance(row, dict) or not row.get("record_key"):
                raise ValueError(f"title QA row {line_number} has no record_key")
            latest[str(row["record_key"])] = row
    return list(latest.values())


def load_title_decisions(path: Path) -> list[dict[str, Any]]:
    payload = _load_json_object(path)
    if set(payload) != {"schema_version", "decisions"}:
        raise ValueError("title decisions must contain only schema_version and decisions")
    if payload.get("schema_version") != TITLE_DECISIONS_SCHEMA_VERSION:
        raise ValueError(
            f"title decisions schema_version must be {TITLE_DECISIONS_SCHEMA_VERSION!r}"
        )
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ValueError("title decisions must contain a decisions array")
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    fields = {"record_key", "title", "evidence"}
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError(f"title decision #{index} has invalid fields")
        record_key = str(raw["record_key"]).strip()
        title = _clean_text(raw["title"])
        if not record_key or not title:
            raise ValueError(f"title decision #{index} has empty record_key/title")
        if record_key in seen:
            raise ValueError(f"duplicate title decision record_key: {record_key}")
        seen.add(record_key)
        decisions.append(
            {
                "record_key": record_key,
                "title": title,
                "evidence": _nonempty_evidence(
                    raw["evidence"], context=f"title decision #{index}"
                ),
            }
        )
    return decisions


def _qa_authors(value: Any) -> list[str]:
    if isinstance(value, list):
        return _clean_string_list(value)
    text = _clean_text(value)
    if not text:
        return []
    if "," in text:
        return [part for part in (_clean_text(item) for item in text.split(",")) if part]
    return [text]


def apply_title_qa(
    records: Sequence[dict[str, Any]],
    qa_rows: Sequence[dict[str, Any]],
    title_decisions: Sequence[dict[str, Any]],
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    qa_by_sha: dict[str, list[dict[str, Any]]] = {}
    qa_by_key: dict[str, dict[str, Any]] = {}
    for row in qa_rows:
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        sha = str(source.get("sha256") or "").casefold()
        key = str(row.get("record_key") or "")
        if not SHA256_RE.fullmatch(sha):
            errors.append(f"title_qa_invalid_sha256:{key}:{sha}")
            continue
        qa_by_sha.setdefault(sha, []).append(row)
        if key in qa_by_key:
            errors.append(f"title_qa_duplicate_record_key:{key}")
        qa_by_key[key] = row
    decisions_by_key = {row["record_key"]: row for row in title_decisions}
    used_qa: set[str] = set()
    used_decisions: set[str] = set()
    accepted_titles = 0
    manual_titles = 0
    accepted_authors = 0
    low_confidence_authors = 0

    for record in records:
        sha = str(record["pdf"].get("sha256") or "").casefold()
        matches = qa_by_sha.get(sha, [])
        if len(matches) != 1:
            errors.append(
                f"title_qa_match_count:{record['paper_id']}:sha256={sha}:actual={len(matches)}"
            )
            continue
        qa = matches[0]
        record_key = str(qa["record_key"])
        used_qa.add(record_key)
        if qa.get("status") != "ok":
            errors.append(f"title_qa_status_not_ok:{record_key}:{qa.get('status')}")
            continue
        title_block = qa.get("title") if isinstance(qa.get("title"), dict) else {}
        title_decision = str(title_block.get("decision") or "")
        canonical_title: str | None = None
        title_source: str
        title_evidence: list[str] = []
        if title_decision == "accepted_high_confidence":
            canonical_title = _clean_text(title_block.get("accepted"))
            title_source = "title_qa_accepted_high_confidence"
            if not canonical_title:
                errors.append(f"title_qa_accepted_title_empty:{record_key}")
                continue
            if record_key in decisions_by_key:
                errors.append(f"title_decision_not_for_low_confidence:{record_key}")
            accepted_titles += 1
        else:
            manual = decisions_by_key.get(record_key)
            if not manual:
                errors.append(f"title_decision_missing:{record_key}:{title_decision}")
                continue
            canonical_title = manual["title"]
            title_source = "manual_title_decision"
            title_evidence = list(manual["evidence"])
            used_decisions.add(record_key)
            manual_titles += 1

        previous_title = _clean_text(record.get("title"))
        aliases = _clean_string_list(record.get("title_aliases"))
        if previous_title and _normalized_title(previous_title) != _normalized_title(
            canonical_title
        ):
            if previous_title not in aliases:
                aliases.append(previous_title)
            record["warnings"].append(
                "canonical_title_mismatch_resolved_by_qa: "
                f"acquisition={previous_title!r}; canonical={canonical_title!r}"
            )
        record["warnings"] = [
            warning
            for warning in record["warnings"]
            if warning
            != "canonical title falls back to a possibly truncated filename; verify it"
        ]
        record["title"] = canonical_title
        record["title_aliases"] = aliases
        record["identity"]["canonical_method_slug"] = _method_slug(canonical_title)
        record["identity"]["canonical_title_source"] = title_source
        record["decision_evidence"].extend(title_evidence)

        author_block = qa.get("authors") if isinstance(qa.get("authors"), dict) else {}
        author_decision = str(author_block.get("decision") or "")
        if author_decision == "accepted_high_confidence":
            authors = _qa_authors(author_block.get("accepted"))
            if not authors:
                errors.append(f"title_qa_accepted_authors_empty:{record_key}")
            else:
                record["authors"] = authors
                accepted_authors += 1
        else:
            record["warnings"].append(
                f"authors_not_overridden_from_low_confidence_title_qa:{author_decision}"
            )
            low_confidence_authors += 1
        record["title_qa"] = {
            "status": title_source,
            "record_key": record_key,
            "source_sha256": sha,
            "title_decision": title_decision,
            "authors_decision": author_decision,
            "manual_evidence": title_evidence,
            "conflicts": qa.get("conflicts", []),
        }
        if previous_title and "emcompress" in previous_title.casefold() and (
            "emcompress" not in canonical_title.casefold()
        ):
            record["code"] = {
                "status": "unknown",
                "url": None,
                "local_path": None,
                "commit": None,
                "license": None,
                "evidence": None,
            }
            record["warnings"].append(
                "EMCompress is an acquisition alias conflicting with the PDF title; "
                "do not infer or attach EMCompress code without separate official evidence"
            )

    unused_qa = sorted(set(qa_by_key) - used_qa)
    unused_decisions = sorted(set(decisions_by_key) - used_decisions)
    if unused_qa:
        errors.append(f"title_qa_unused_records:{','.join(unused_qa[:20])}")
    if unused_decisions:
        errors.append(f"title_decisions_unused:{','.join(unused_decisions)}")
    empty_titles = [
        record["paper_id"] for record in records if not _clean_text(record.get("title"))
    ]
    if empty_titles:
        errors.append(f"canonical_titles_empty:{','.join(empty_titles)}")
    truncated_warnings = sum(
        "possibly truncated filename" in str(warning)
        for record in records
        for warning in record["warnings"]
    )
    if truncated_warnings:
        errors.append(f"canonical_title_truncation_warnings_remaining:{truncated_warnings}")
    return errors, {
        "qa_records": len(qa_rows),
        "qa_matched": len(used_qa),
        "accepted_high_confidence_titles": accepted_titles,
        "manual_title_decisions": manual_titles,
        "accepted_high_confidence_authors": accepted_authors,
        "low_confidence_authors_preserved": low_confidence_authors,
        "empty_titles": len(empty_titles),
        "truncation_warnings_remaining": truncated_warnings,
    }


def load_author_resolution(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid author-resolution JSONL at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict) or not row.get("record_key"):
                raise ValueError(f"author-resolution row {line_number} has no record_key")
            key = str(row["record_key"])
            latest[key] = row
    return list(latest.values())


def _validated_resolved_authors(
    row: dict[str, Any], *, record_key: str
) -> tuple[list[str], str | None, bool]:
    authors = row.get("authors")
    authors_display = row.get("authors_display")
    if authors is not None and authors_display is not None:
        raise ValueError(f"author resolution has both authors and authors_display: {record_key}")
    if authors is not None:
        if (
            not isinstance(authors, list)
            or not authors
            or any(not isinstance(item, str) or not item.strip() for item in authors)
        ):
            raise ValueError(f"author resolution has invalid authors array: {record_key}")
        return list(authors), None, False
    if not isinstance(authors_display, str) or not authors_display.strip():
        raise ValueError(f"author resolution has no non-empty author value: {record_key}")
    # Hub currently consumes an array.  Preserve the unresolved display string as one
    # element; never invent a delimiter-based split.
    return [authors_display], authors_display, True


def apply_author_resolution(
    records: Sequence[dict[str, Any]], resolution_rows: Sequence[dict[str, Any]]
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    by_sha: dict[str, list[dict[str, Any]]] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for row in resolution_rows:
        key = str(row.get("record_key") or "")
        sha = str(row.get("pdf_sha256") or "").casefold()
        if not key or not SHA256_RE.fullmatch(sha):
            errors.append(f"author_resolution_invalid_key_or_sha:{key}:{sha}")
            continue
        if key in by_key:
            errors.append(f"author_resolution_duplicate_record_key:{key}")
        by_key[key] = row
        by_sha.setdefault(sha, []).append(row)

    used: set[str] = set()
    structured = 0
    display_fallbacks = 0
    empty = 0
    for record in records:
        sha = str(record["pdf"].get("sha256") or "").casefold()
        matches = by_sha.get(sha, [])
        if len(matches) != 1:
            errors.append(
                "author_resolution_match_count:"
                f"paper_id={record['paper_id']}:sha256={sha}:actual={len(matches)}"
            )
            empty += 1
            continue
        row = matches[0]
        key = str(row["record_key"])
        qa_key = str((record.get("title_qa") or {}).get("record_key") or "")
        if qa_key and key != qa_key:
            errors.append(
                f"author_resolution_record_key_mismatch:{record['paper_id']}:"
                f"qa={qa_key}:author={key}"
            )
            empty += 1
            continue
        status = _clean_text(row.get("status"))
        evidence = row.get("evidence")
        if not status or not isinstance(evidence, dict):
            errors.append(f"author_resolution_missing_status_or_evidence:{key}")
            empty += 1
            continue
        raw = evidence.get("raw")
        try:
            authors, authors_display, display_fallback = _validated_resolved_authors(
                row, record_key=key
            )
        except ValueError as exc:
            errors.append(str(exc))
            empty += 1
            continue
        record["authors"] = authors
        record["authors_display"] = authors_display
        record["display_fallback"] = display_fallback
        record["author_resolution"] = {
            "record_key": key,
            "pdf_sha256": sha,
            "status": status,
            "raw": raw,
            "evidence": evidence,
            "display_fallback": display_fallback,
        }
        record["decision_evidence"].append(
            f"author resolution: {key} ({status})"
        )
        used.add(key)
        if display_fallback:
            display_fallbacks += 1
        else:
            structured += 1

    unused = sorted(set(by_key) - used)
    if unused:
        errors.append(f"author_resolution_unused_records:{','.join(unused[:20])}")
    final_empty = sum(not record.get("authors") for record in records)
    if final_empty:
        errors.append(f"author_resolution_final_empty:{final_empty}")
    return errors, {
        "resolution_records": len(resolution_rows),
        "matched": len(used),
        "structured_authors": structured,
        "display_fallbacks": display_fallbacks,
        "empty_or_invalid": empty,
        "final_empty": final_empty,
    }


def load_routing_decisions(
    path: Path, *, allowed_domains: set[str]
) -> list[dict[str, Any]]:
    payload = _load_json_object(path)
    allowed_top = {"schema_version", "decisions"}
    extra_top = sorted(set(payload) - allowed_top)
    if extra_top:
        raise ValueError(f"routing decisions contain unknown top-level fields: {extra_top}")
    if payload.get("schema_version") != ROUTING_DECISIONS_SCHEMA_VERSION:
        raise ValueError(
            "routing decisions schema_version must be "
            f"{ROUTING_DECISIONS_SCHEMA_VERSION!r}"
        )
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ValueError("routing decisions must contain a decisions array")
    fields = {"paper_id", "source_sha256", "target_domain", "evidence"}
    decisions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"routing decision #{index} is not an object")
        missing = sorted(fields - set(raw))
        extra = sorted(set(raw) - fields)
        if missing or extra:
            raise ValueError(
                f"routing decision #{index} fields invalid: missing={missing}, extra={extra}"
            )
        paper_id = str(raw["paper_id"]).strip()
        source_sha256 = str(raw["source_sha256"]).strip().casefold()
        target_domain = str(raw["target_domain"]).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,127}", paper_id):
            raise ValueError(f"routing decision #{index} has invalid paper_id")
        if not SHA256_RE.fullmatch(source_sha256):
            raise ValueError(f"routing decision #{index} has invalid source_sha256")
        if target_domain not in allowed_domains or target_domain == "needs-review":
            raise ValueError(
                f"routing decision #{index} target_domain is not an existing taxonomy slug: "
                f"{target_domain}"
            )
        evidence = _nonempty_evidence(raw["evidence"], context=f"routing decision #{index}")
        key = (paper_id, source_sha256)
        if key in seen:
            raise ValueError(
                f"duplicate routing decision key: paper_id={paper_id}, "
                f"source_sha256={source_sha256}"
            )
        seen.add(key)
        decisions.append(
            {
                "paper_id": paper_id,
                "source_sha256": source_sha256,
                "target_domain": target_domain,
                "evidence": evidence,
            }
        )
    return decisions


def apply_routing_decisions(
    records: Sequence[dict[str, Any]],
    actions: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    *,
    core_root: Path,
) -> tuple[list[str], int]:
    errors: list[str] = []
    applied = 0
    records_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    actions_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (record["paper_id"], str(record["pdf"].get("sha256") or "").casefold())
        records_by_key.setdefault(key, []).append(record)
    for action in actions:
        key = (
            action["paper_id"],
            str(action.get("source_sha256") or "").casefold(),
        )
        actions_by_key.setdefault(key, []).append(action)

    for decision in decisions:
        key = (decision["paper_id"], decision["source_sha256"])
        matched_records = records_by_key.get(key, [])
        matched_actions = actions_by_key.get(key, [])
        if len(matched_records) != 1 or len(matched_actions) != 1:
            errors.append(
                "routing_decision_match_count:"
                f"paper_id={key[0]}:sha256={key[1]}:"
                f"records={len(matched_records)}:actions={len(matched_actions)}"
            )
            continue
        record = matched_records[0]
        action = matched_actions[0]
        if record["domain_slugs"] != ["needs-review"] or action["domain"] != "needs-review":
            errors.append(
                "routing_decision_not_for_needs_review:"
                f"paper_id={key[0]}:sha256={key[1]}:"
                f"current_domain={action['domain']}"
            )
            continue
        target_domain = decision["target_domain"]
        targets = _targets(core_root, target_domain, record["paper_id"])
        previous_routing = dict(record["routing"])
        record["domain_slugs"] = [target_domain]
        record["target_pdf_path"] = targets["pdf"]
        record["target_html_path"] = targets["html"]
        record["target_metadata_path"] = targets["metadata"]
        record["pdf"]["path"] = targets["html_pdf_link"]
        record["tags"] = list(dict.fromkeys([*record["tags"], target_domain]))
        record["routing"] = {
            "domain": target_domain,
            "rule_ids": ["explicit-routing-decision"],
            "reason": "Explicit evidence-backed routing decision replaced needs-review.",
            "confidence": "explicit-human-decision",
            "previous": previous_routing,
            "evidence": list(decision["evidence"]),
        }
        record["routing_decision"] = dict(decision)
        record["decision_evidence"].extend(decision["evidence"])
        action.update(
            {
                "domain": target_domain,
                "target_path": targets["pdf"],
                "reason": "Explicit routing decision resolved needs-review.",
                "routing_decision": dict(decision),
            }
        )
        applied += 1
    return errors, applied


def _metadata_values(record: dict[str, Any], source_path: Path) -> dict[str, Any]:
    file_fields = filename_fields(source_path)
    warnings = _clean_string_list(record.get("warnings"))
    title_fact = _clean_text(_fact_raw(record, "title"))
    title = title_fact or _clean_text(file_fields["title"])
    title_source = "metadata_fact" if title_fact else "filename_fallback_needs_review"
    if not title_fact:
        warnings.append("canonical title falls back to a possibly truncated filename; verify it")
    authors = _clean_string_list(_fact_raw(record, "authors"))
    if not authors:
        warnings.append("canonical authors are missing and require evidence review")
    fact_year = _fact_raw(record, "year")
    try:
        year = int(fact_year) if fact_year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
        warnings.append(f"invalid metadata year: {fact_year}")
    if year is None and file_fields["year"] is not None:
        year = int(file_fields["year"])
        warnings.append("year falls back to filename classification")
    if year and file_fields["year"] and year != file_fields["year"]:
        warnings.append(f"metadata year {year} conflicts with filename year {file_fields['year']}")
    venue = _clean_text(_fact_raw(record, "venue")) or _clean_text(file_fields["venue"])
    if not _fact_raw(record, "venue") and venue:
        warnings.append("venue falls back to filename classification")
    arxiv_id, arxiv_warnings = _single_identifier(record, "arxiv_id")
    doi, doi_warnings = _single_identifier(record, "doi")
    warnings.extend(arxiv_warnings)
    warnings.extend(doi_warnings)
    return {
        "title": title,
        "title_source": title_source,
        "authors": authors,
        "year": year,
        "venue": venue,
        "arxiv_id": arxiv_id,
        "doi": doi,
        "abstract": _clean_text(_fact_raw(record, "abstract")),
        "filename_method": _clean_text(file_fields["method"]),
        "warnings": list(dict.fromkeys(warnings)),
    }


def _metadata_for_recovery(
    map_record: dict[str, Any], evidence: dict[str, Any], metadata: dict[str, Any] | None
) -> dict[str, Any]:
    result = dict(metadata or {})
    source = dict(result.get("source") or {})
    source.update(
        {
            "path": evidence.get("path"),
            "sha256": evidence.get("sha256"),
            "size": evidence.get("size"),
        }
    )
    result["source"] = source
    parser = dict(result.get("parser") or {})
    if evidence.get("pages"):
        parser["page_count"] = evidence["pages"]
    result["parser"] = parser
    result.setdefault("facts", {})
    result["status"] = "ok" if evidence.get("path") else "error"
    if not evidence.get("path"):
        result["error"] = {"message": "recovered PDF path is absent"}
    result["recovery_map_record"] = map_record
    result["recovery_evidence"] = evidence
    return result


def _record_from_metadata(
    metadata: dict[str, Any],
    *,
    source_group: str,
    core_root: Path,
    vlm_entries: Sequence[dict[str, Any]],
    recovery_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    errors: list[str] = []
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    if not source.get("path"):
        raise ValueError("metadata record has no source path")
    source_path = Path(str(source["path"]))
    values = _metadata_values(metadata, source_path)
    source_urls: list[dict[str, str]] = []
    if recovery_override:
        title = _clean_text(recovery_override.get("title"))
        if title:
            values["title"] = title
            values["title_source"] = "verified_recovery_map"
            values["filename_method"] = _method_slug(title)
        for field in ("arxiv_id", "doi"):
            normalized = (
                _normalize_arxiv(recovery_override.get(field))
                if field == "arxiv_id"
                else _normalize_doi(recovery_override.get(field))
            )
            if normalized:
                values[field] = normalized
        markdown = _clean_text(recovery_override.get("markdown"))
        if markdown:
            filename = Path(markdown).name
            recovered_fields = filename_fields(Path(filename))
            if recovered_fields["year"]:
                values["year"] = int(recovered_fields["year"])
            if recovered_fields["venue"]:
                values["venue"] = _clean_text(recovered_fields["venue"])
        for label, key in (
            ("Official landing page", "official_landing_url"),
            ("Official PDF", "official_pdf_url"),
        ):
            url = _clean_text(recovery_override.get(key))
            if url:
                source_urls.append({"label": label, "url": url})
    if values["arxiv_id"] and not any("arxiv.org" in item["url"] for item in source_urls):
        source_urls.append(
            {"label": "arXiv", "url": f"https://arxiv.org/abs/{values['arxiv_id']}"}
        )
    if values["doi"]:
        source_urls.append({"label": "DOI", "url": f"https://doi.org/{values['doi']}"})

    try:
        info = inspect_pdf(source_path)
    except (OSError, ValueError) as exc:
        errors.append(f"source_pdf_invalid:{source_path}:{exc}")
        info = FileInfo(
            sha256=_clean_text(source.get("sha256")) or "",
            size=int(source.get("size") or 0),
            page_count=int((metadata.get("parser") or {}).get("page_count") or 0),
        )
    expected_sha = _clean_text(source.get("sha256"))
    if expected_sha and info.sha256 and expected_sha.casefold() != info.sha256:
        errors.append(
            f"metadata_sha256_mismatch:{source_path}:expected={expected_sha}:actual={info.sha256}"
        )
    if recovery_override and metadata.get("recovery_evidence"):
        recovery_sha = _clean_text(metadata["recovery_evidence"].get("sha256"))
        if recovery_sha and info.sha256 and recovery_sha.casefold() != info.sha256:
            errors.append(
                f"recovery_sha256_mismatch:{source_path}:expected={recovery_sha}:actual={info.sha256}"
            )

    title = values["title"]
    if not title:
        errors.append(f"canonical_title_missing:{source_path}")
        title = source_path.stem
    route = route_domain(source_group, title, source_path.name)
    method = _method_slug(title, values["filename_method"])
    try:
        paper_id = stable_paper_id(
            arxiv_id=values["arxiv_id"],
            title=title,
            year=values["year"],
            filename_method=values["filename_method"],
        )
    except ValueError as exc:
        fallback = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:12]
        paper_id = f"unresolved-{fallback}"
        errors.append(f"stable_id_missing:{source_path}:{exc}")
    targets = _targets(core_root, route["domain"], paper_id)
    record: dict[str, Any] = {
        "paper_id": paper_id,
        "source_record_id": paper_id,
        "canonical_paper_id": paper_id,
        "existing_vlm_id": None,
        "version_relation": None,
        "routing_decision": None,
        "source_group": source_group,
        "disposition": (
            "include-main"
            if source_group
            in {
                "video-main-current",
                "video-main-recovered",
                "embodied-main",
                "navigation-main",
            }
            else "reclassify-retain"
        ),
        "title": title,
        "title_zh": None,
        "authors": values["authors"],
        "year": values["year"],
        "venue": values["venue"],
        "doi": values["doi"],
        "arxiv_id": values["arxiv_id"],
        "abstract": values["abstract"],
        "summary": None,
        "tags": list(
            dict.fromkeys(
                ["intake-2026-08-31", source_group, route["domain"], "compression-audit"]
            )
        ),
        "domain_slugs": [route["domain"]],
        "source_urls": source_urls,
        "source_pdf_path": str(source_path.resolve(strict=False)),
        "target_pdf_path": targets["pdf"],
        "target_html_path": targets["html"],
        "target_metadata_path": targets["metadata"],
        "pdf": {
            "path": targets["html_pdf_link"],
            "sha256": info.sha256 or None,
            "size": info.size or None,
            "page_count": info.page_count or None,
            "version": "recovered-official" if recovery_override else "desktop-acquisition",
        },
        "analysis": None,
        "code": {
            "status": "unknown",
            "url": None,
            "local_path": None,
            "commit": None,
            "license": None,
            "evidence": None,
        },
        "identity": {
            "id_policy": "arxiv-first-else-method-year",
            "method_slug": method,
            "title_source": values["title_source"],
        },
        "routing": route,
        "existing_vlm_matches": [],
        "decision_evidence": [
            f"source group: {source_group}",
            f"route rules: {', '.join(route['rule_ids'])}",
            f"title source: {values['title_source']}",
        ],
        "warnings": list(values["warnings"]),
    }
    matches = _vlm_matches(record, vlm_entries)
    record["existing_vlm_matches"] = matches

    action = "copy_new"
    reason = "No existing VLM exact representation was selected; copy without overwrite."
    target = targets["pdf"]
    if route["domain"] == "video-llm-compression" and matches:
        exact = [item for item in matches if item["classification"] == "duplicate_exact_hash"]
        review = [item for item in matches if item["classification"] != "duplicate_exact_hash"]
        if len(exact) == 1:
            target = exact[0]["existing_pdf_path"]
            if not target:
                errors.append(f"existing_vlm_pdf_path_missing:{exact[0]['existing_id']}")
                action = "blocked"
                reason = "Exact VLM hash match has no local PDF path."
                target = targets["pdf"]
            else:
                action = "reuse_existing_vlm"
                reason = (
                    "Existing VLM PDF has the same SHA-256; do not create a duplicate copy."
                )
            existing_id = str(exact[0]["existing_id"])
            if action == "reuse_existing_vlm":
                record["canonical_paper_id"] = existing_id
                record["existing_vlm_id"] = existing_id
                record["target_pdf_path"] = target
                record["pdf"]["path"] = _relative_file_link(
                    Path(str(target)), Path(str(record["target_html_path"]))
                )
                record["version_relation"] = {
                    "relation_type": "same_representation_as",
                    "canonical_paper_id": existing_id,
                    "existing_vlm_id": existing_id,
                    "source_sha256": info.sha256,
                }
        elif len(exact) > 1:
            errors.append(f"multiple_vlm_hash_matches:{paper_id}")
            action = "blocked"
            reason = "Multiple existing VLM records share this input hash."
        elif review:
            action = "hold_identity_review"
            reason = (
                "Existing VLM identity/title candidate has different bytes; "
                "review as a version."
            )
    elif any(
        item["classification"] == "version_candidate_strong_identity" for item in matches
    ):
        action = "hold_identity_review"
        reason = (
            "A strong VLM identity candidate has different bytes; "
            "review cross-domain versioning."
        )

    if errors:
        action = "blocked"
    copy_action = {
        "paper_id": paper_id,
        "source_group": source_group,
        "domain": route["domain"],
        "source_path": str(source_path.resolve(strict=False)),
        "source_sha256": info.sha256 or None,
        "source_size": info.size or None,
        "source_page_count": info.page_count or None,
        "target_path": target,
        "planned_action": action,
        "reason": reason,
        "apply_status": "dry_run",
        "identity_decision": None,
        "routing_decision": None,
        "existing_vlm_id": record["existing_vlm_id"],
        "existing_versions_dir": None,
        "version_label": None,
        "target_relative_path": None,
        "errors": errors,
    }
    return record, copy_action, errors


def _match_recovery_metadata(
    metadata_rows: Sequence[dict[str, Any]], evidence: dict[str, Any]
) -> dict[str, Any] | None:
    path = evidence.get("path")
    if not path:
        return None
    key = os.path.normcase(str(path))
    for row in metadata_rows:
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        if os.path.normcase(str(source.get("path") or "")) == key:
            return row
    return None


def _detect_id_collisions(records: Sequence[dict[str, Any]]) -> list[str]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_id.setdefault(record["paper_id"], []).append(record)
    errors: list[str] = []
    for paper_id, rows in sorted(by_id.items()):
        if len(rows) < 2:
            continue
        paths = " | ".join(str(row["source_pdf_path"]) for row in rows)
        errors.append(f"paper_id_collision:{paper_id}:{paths}")
    targets: dict[str, list[str]] = {}
    for record in records:
        targets.setdefault(os.path.normcase(record["target_pdf_path"]), []).append(
            record["paper_id"]
        )
    for target, paper_ids in sorted(targets.items()):
        if len(paper_ids) > 1:
            errors.append(f"target_path_collision:{target}:{','.join(paper_ids)}")
    return errors


def build_intake(
    *,
    metadata_path: Path,
    recovery_map_path: Path,
    desktop_root: Path,
    vlm_catalog_path: Path,
    core_root: Path,
    recovery_report_path: Path | None = None,
    recovered_root: Path | None = None,
    identity_decisions_path: Path | None = None,
    routing_decisions_path: Path | None = None,
    taxonomy_path: Path | None = None,
    title_qa_results_path: Path | None = None,
    title_decisions_path: Path | None = None,
    author_resolution_path: Path | None = None,
    expected_count: int = 376,
    validate_batch_shape: bool = True,
) -> BuildResult:
    metadata_rows = load_metadata(metadata_path)
    vlm_unit_root, vlm_entries = _existing_vlm_entries(vlm_catalog_path)
    errors: list[str] = []
    identity_decisions: list[dict[str, Any]] = []
    routing_decisions: list[dict[str, Any]] = []
    title_qa_rows: list[dict[str, Any]] = []
    title_decisions: list[dict[str, Any]] = []
    author_resolution_rows: list[dict[str, Any]] = []
    resolved_taxonomy_path = taxonomy_path or DEFAULT_TAXONOMY_PATH
    if identity_decisions_path:
        try:
            identity_decisions = load_identity_decisions(identity_decisions_path)
        except ValueError as exc:
            errors.append(f"identity_decisions_invalid:{exc}")
    if routing_decisions_path:
        try:
            routing_decisions = load_routing_decisions(
                routing_decisions_path,
                allowed_domains=load_taxonomy_domains(resolved_taxonomy_path),
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"routing_decisions_invalid:{exc}")
    if title_decisions_path and not title_qa_results_path:
        errors.append("title_decisions_require_title_qa_results")
    if title_qa_results_path:
        try:
            title_qa_rows = load_title_qa_results(title_qa_results_path)
            if title_decisions_path:
                title_decisions = load_title_decisions(title_decisions_path)
        except (OSError, ValueError) as exc:
            errors.append(f"title_qa_inputs_invalid:{exc}")
    if author_resolution_path:
        try:
            author_resolution_rows = load_author_resolution(author_resolution_path)
        except (OSError, ValueError) as exc:
            errors.append(f"author_resolution_invalid:{exc}")
    records: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    used_metadata_paths: set[str] = set()

    for metadata in metadata_rows:
        source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
        if not source.get("path"):
            errors.append("metadata_record_without_source_path")
            continue
        path = Path(str(source["path"]))
        if not _is_within(path, desktop_root):
            continue
        try:
            group = source_group_for(path, desktop_root)
            record, action, row_errors = _record_from_metadata(
                metadata,
                source_group=group,
                core_root=core_root,
                vlm_entries=vlm_entries,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        records.append(record)
        actions.append(action)
        errors.extend(row_errors)
        used_metadata_paths.add(os.path.normcase(str(path)))

    recovery_rows = _recovery_sources(
        recovery_map_path,
        recovery_report_path,
        recovered_root,
    )
    for map_record, evidence in recovery_rows:
        matched = _match_recovery_metadata(metadata_rows, evidence)
        metadata = _metadata_for_recovery(map_record, evidence, matched)
        if matched:
            matched_source = (
                matched.get("source") if isinstance(matched.get("source"), dict) else {}
            )
            if matched_source.get("path"):
                used_metadata_paths.add(os.path.normcase(str(matched_source["path"])))
        try:
            record, action, row_errors = _record_from_metadata(
                metadata,
                source_group="video-main-recovered",
                core_root=core_root,
                vlm_entries=vlm_entries,
                recovery_override=map_record,
            )
        except ValueError as exc:
            errors.append(f"recovery_record_{map_record.get('index')}:{exc}")
            continue
        records.append(record)
        actions.append(action)
        errors.extend(row_errors)

    extra_metadata: list[str] = []
    recovery_paths = {
        os.path.normcase(str(evidence.get("path")))
        for _, evidence in recovery_rows
        if evidence.get("path")
    }
    for metadata in metadata_rows:
        source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
        source_path = source.get("path")
        if not source_path:
            continue
        key = os.path.normcase(str(source_path))
        if key not in used_metadata_paths and key not in recovery_paths:
            extra_metadata.append(str(source_path))
    if extra_metadata:
        errors.append(
            "metadata_paths_outside_acquisition_or_recovery:" + " | ".join(extra_metadata[:10])
        )

    if title_qa_results_path:
        title_qa_errors, title_qa_summary = apply_title_qa(
            records, title_qa_rows, title_decisions
        )
        errors.extend(title_qa_errors)
    else:
        title_qa_summary = {
            "qa_records": 0,
            "qa_matched": 0,
            "accepted_high_confidence_titles": 0,
            "manual_title_decisions": 0,
            "accepted_high_confidence_authors": 0,
            "low_confidence_authors_preserved": 0,
            "empty_titles": sum(not _clean_text(record.get("title")) for record in records),
            "truncation_warnings_remaining": sum(
                "possibly truncated filename" in str(warning)
                for record in records
                for warning in record["warnings"]
            ),
        }
    if author_resolution_path:
        author_resolution_errors, author_resolution_summary = apply_author_resolution(
            records, author_resolution_rows
        )
        errors.extend(author_resolution_errors)
    else:
        author_resolution_summary = {
            "resolution_records": 0,
            "matched": 0,
            "structured_authors": 0,
            "display_fallbacks": 0,
            "empty_or_invalid": len(records),
            "final_empty": sum(not record.get("authors") for record in records),
        }

    routing_decision_errors, routing_decisions_applied = apply_routing_decisions(
        records,
        actions,
        routing_decisions,
        core_root=core_root,
    )
    errors.extend(routing_decision_errors)
    identity_decision_errors, identity_decisions_applied = apply_identity_decisions(
        records,
        actions,
        identity_decisions,
        vlm_unit_root=vlm_unit_root,
    )
    errors.extend(identity_decision_errors)

    collision_errors = _detect_id_collisions(records)
    errors.extend(collision_errors)
    group_counts = Counter(record["source_group"] for record in records)
    if len(records) != expected_count:
        errors.append(f"expected_count_mismatch:expected={expected_count}:actual={len(records)}")
    if validate_batch_shape:
        for group, expected in BATCH_GROUP_COUNTS.items():
            actual = group_counts.get(group, 0)
            if actual != expected:
                errors.append(
                    f"source_group_count_mismatch:{group}:expected={expected}:actual={actual}"
                )
        unexpected = sorted(set(group_counts) - set(BATCH_GROUP_COUNTS))
        if unexpected:
            errors.append(f"unexpected_source_groups:{','.join(unexpected)}")

    errors = list(dict.fromkeys(errors))
    records.sort(key=lambda row: (row["source_group"], row["paper_id"], row["source_pdf_path"]))
    actions.sort(key=lambda row: (row["source_group"], row["paper_id"], row["source_path"]))
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "mode": "dry-run",
        "status": "blocked" if errors else "ready",
        "inputs": {
            "metadata": str(metadata_path.resolve()),
            "video_recovery_map": str(recovery_map_path.resolve()),
            "video_recovery_report": (
                str((recovery_report_path or _recovery_report_default(recovery_map_path)).resolve())
                if recovery_report_path or _recovery_report_default(recovery_map_path)
                else None
            ),
            "desktop_root": str(desktop_root.resolve()),
            "vlm_catalog": str(vlm_catalog_path.resolve()),
            "core_root": str(core_root.resolve()),
            "identity_decisions": (
                str(identity_decisions_path.resolve()) if identity_decisions_path else None
            ),
            "routing_decisions": (
                str(routing_decisions_path.resolve()) if routing_decisions_path else None
            ),
            "taxonomy": str(resolved_taxonomy_path.resolve()),
            "title_qa_results": (
                str(title_qa_results_path.resolve()) if title_qa_results_path else None
            ),
            "title_decisions": (
                str(title_decisions_path.resolve()) if title_decisions_path else None
            ),
            "author_resolution": (
                str(author_resolution_path.resolve()) if author_resolution_path else None
            ),
        },
        "expected_count": expected_count,
        "record_count": len(records),
        "source_group_counts": dict(sorted(group_counts.items())),
        "domain_counts": dict(
            sorted(Counter(record["domain_slugs"][0] for record in records).items())
        ),
        "copy_action_counts": dict(
            sorted(Counter(action["planned_action"] for action in actions).items())
        ),
        "vlm_match_counts": dict(
            sorted(
                Counter(
                    match["classification"]
                    for record in records
                    for match in record["existing_vlm_matches"]
                ).items()
            )
        ),
        "metadata_review_count": sum(bool(record["warnings"]) for record in records),
        "identity_decisions": {
            "provided": len(identity_decisions),
            "applied": identity_decisions_applied,
            "unresolved_holds": sum(
                action["planned_action"] == "hold_identity_review" for action in actions
            ),
        },
        "routing_decisions": {
            "provided": len(routing_decisions),
            "applied": routing_decisions_applied,
            "unresolved_needs_review": sum(
                action["domain"] == "needs-review" for action in actions
            ),
        },
        "title_qa": {
            **title_qa_summary,
            "required_for_apply": expected_count == 376,
            "complete": bool(title_qa_results_path)
            and title_qa_summary["qa_matched"] == len(records)
            and title_qa_summary["empty_titles"] == 0
            and title_qa_summary["truncation_warnings_remaining"] == 0,
        },
        "author_resolution": {
            **author_resolution_summary,
            "required_for_apply": expected_count == 376,
            "complete": bool(author_resolution_path)
            and author_resolution_summary["matched"] == len(records)
            and author_resolution_summary["final_empty"] == 0
            and author_resolution_summary["empty_or_invalid"] == 0,
        },
        "errors": errors,
    }
    return BuildResult(report=report, records=records, copy_actions=actions)


def _verify_existing_target(action: dict[str, Any]) -> str:
    target = Path(action["target_path"])
    expected = action.get("source_sha256")
    if not target.is_file():
        raise ValueError(f"expected reused target is missing: {target}")
    actual = sha256_file(target)
    if not expected or actual != expected:
        raise ValueError(
            f"reused target hash mismatch: {target}: expected={expected}, actual={actual}"
        )
    return actual


def _copy_no_overwrite(source: Path, target: Path, expected_sha256: str) -> str:
    current_source_hash = sha256_file(source)
    if current_source_hash != expected_sha256:
        raise ValueError(
            f"source changed since plan: {source}: expected={expected_sha256}, "
            f"actual={current_source_hash}"
        )
    if target.exists():
        if not target.is_file():
            raise ValueError(f"target exists and is not a file: {target}")
        target_hash = sha256_file(target)
        if target_hash != expected_sha256:
            raise FileExistsError(
                f"refusing to overwrite different target: {target}: "
                f"expected={expected_sha256}, actual={target_hash}"
            )
        return "already_present_verified"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".partial", dir=target.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        copied_hash = sha256_file(temporary)
        if copied_hash != expected_sha256:
            raise OSError(
                f"temporary copy hash mismatch: {temporary}: "
                f"expected={expected_sha256}, actual={copied_hash}"
            )
        try:
            os.link(temporary, target)
        except FileExistsError as link_error:
            target_hash = sha256_file(target)
            if target_hash != expected_sha256:
                raise FileExistsError(
                    f"target appeared with different bytes: {target}: "
                    f"expected={expected_sha256}, actual={target_hash}"
                ) from link_error
            return "already_present_verified"
        except OSError as link_error:
            # Filesystems without hard-link support still get exclusive creation.
            try:
                with temporary.open("rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
            except FileExistsError:
                target_hash = sha256_file(target)
                if target_hash != expected_sha256:
                    raise
                return "already_present_verified"
            target_hash = sha256_file(target)
            if target_hash != expected_sha256:
                target.unlink(missing_ok=True)
                raise OSError(
                    f"final target hash mismatch: {target}: "
                    f"expected={expected_sha256}, actual={target_hash}"
                ) from link_error
        return "copied_verified"
    finally:
        temporary.unlink(missing_ok=True)


def apply_copy_plan(result: BuildResult, *, core_root: Path) -> BuildResult:
    if not result.ready:
        raise ValueError("copy plan is blocked; resolve report errors before --apply")
    root = core_root.resolve(strict=True)
    preflight_errors: list[str] = []
    title_qa = result.report.get("title_qa", {})
    if title_qa.get("required_for_apply") and not title_qa.get("complete"):
        preflight_errors.append(
            "title_qa_incomplete: expected full SHA/record_key coverage, non-empty canonical "
            "titles, and zero truncation warnings"
        )
    author_resolution = result.report.get("author_resolution", {})
    if author_resolution.get("required_for_apply") and not author_resolution.get("complete"):
        preflight_errors.append(
            "author_resolution_incomplete: expected 376/376 record_key+SHA matches "
            "with zero empty author values"
        )
    for action in result.copy_actions:
        if action["domain"] == "needs-review":
            preflight_errors.append(
                "unresolved_routing_needs_review:"
                f"paper_id={action['paper_id']}:sha256={action.get('source_sha256')}"
            )
            continue
        target = Path(action["target_path"])
        if not _is_within(target, root):
            preflight_errors.append(f"target_outside_core:{target}")
            continue
        planned = action["planned_action"]
        if planned == "hold_identity_review":
            preflight_errors.append(
                "unresolved_identity_hold:"
                f"paper_id={action['paper_id']}:sha256={action.get('source_sha256')}"
            )
            continue
        if planned in {"copy_new", "copy_alternate_version"}:
            if planned == "copy_alternate_version":
                versions_dir_raw = action.get("existing_versions_dir")
                decision = action.get("identity_decision")
                if not versions_dir_raw or not isinstance(decision, dict):
                    preflight_errors.append(
                        f"alternate_version_decision_missing:{action['paper_id']}"
                    )
                    continue
                versions_dir = Path(str(versions_dir_raw)).resolve(strict=False)
                if target == versions_dir or not _is_within(target, versions_dir):
                    preflight_errors.append(
                        f"alternate_version_target_outside_versions:{target}:{versions_dir}"
                    )
                    continue
            source = Path(action["source_path"])
            expected = action.get("source_sha256")
            if not source.is_file() or not expected:
                preflight_errors.append(f"copy_source_unavailable:{source}")
                continue
            actual = sha256_file(source)
            if actual != expected:
                preflight_errors.append(
                    f"copy_source_changed:{source}:expected={expected}:actual={actual}"
                )
            if target.exists():
                if not target.is_file():
                    preflight_errors.append(f"target_not_file:{target}")
                else:
                    target_hash = sha256_file(target)
                    if target_hash != expected:
                        preflight_errors.append(
                            f"target_conflict:{target}:expected={expected}:actual={target_hash}"
                        )
        elif planned == "reuse_existing_vlm":
            try:
                _verify_existing_target(action)
            except ValueError as exc:
                preflight_errors.append(str(exc))
    if preflight_errors:
        raise ValueError("apply preflight failed:\n" + "\n".join(preflight_errors))

    for action in result.copy_actions:
        planned = action["planned_action"]
        if planned in {"copy_new", "copy_alternate_version"}:
            action["apply_status"] = _copy_no_overwrite(
                Path(action["source_path"]),
                Path(action["target_path"]),
                str(action["source_sha256"]),
            )
        elif planned == "reuse_existing_vlm":
            _verify_existing_target(action)
            action["apply_status"] = "reused_existing_verified"
        else:
            action["apply_status"] = "skipped"
    result.report["mode"] = "apply"
    result.report["apply_status_counts"] = dict(
        sorted(Counter(action["apply_status"] for action in result.copy_actions).items())
    )
    return result


PAPER_CSV_FIELDS = (
    "paper_id",
    "source_record_id",
    "canonical_paper_id",
    "existing_vlm_id",
    "version_relation_json",
    "routing_decision_json",
    "title_aliases_json",
    "title_qa_json",
    "authors_display",
    "display_fallback",
    "author_resolution_json",
    "source_group",
    "disposition",
    "title",
    "authors_json",
    "year",
    "venue",
    "doi",
    "arxiv_id",
    "domain_slugs_json",
    "source_pdf_path",
    "target_pdf_path",
    "target_html_path",
    "sha256",
    "size",
    "page_count",
    "routing_rule_ids_json",
    "vlm_matches_json",
    "warnings_json",
)
COPY_CSV_FIELDS = (
    "paper_id",
    "source_group",
    "domain",
    "source_path",
    "source_sha256",
    "source_size",
    "source_page_count",
    "target_path",
    "planned_action",
    "existing_vlm_id",
    "version_label",
    "target_relative_path",
    "identity_decision_json",
    "routing_decision_json",
    "apply_status",
    "reason",
    "errors_json",
)


def _paper_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_id": record["paper_id"],
        "source_record_id": record["source_record_id"],
        "canonical_paper_id": record["canonical_paper_id"],
        "existing_vlm_id": record["existing_vlm_id"],
        "version_relation_json": json.dumps(record["version_relation"], ensure_ascii=False),
        "routing_decision_json": json.dumps(record["routing_decision"], ensure_ascii=False),
        "title_aliases_json": json.dumps(record.get("title_aliases", []), ensure_ascii=False),
        "title_qa_json": json.dumps(record.get("title_qa"), ensure_ascii=False),
        "authors_display": record.get("authors_display"),
        "display_fallback": record.get("display_fallback", False),
        "author_resolution_json": json.dumps(
            record.get("author_resolution"), ensure_ascii=False
        ),
        "source_group": record["source_group"],
        "disposition": record["disposition"],
        "title": record["title"],
        "authors_json": json.dumps(record["authors"], ensure_ascii=False),
        "year": record["year"],
        "venue": record["venue"],
        "doi": record["doi"],
        "arxiv_id": record["arxiv_id"],
        "domain_slugs_json": json.dumps(record["domain_slugs"], ensure_ascii=False),
        "source_pdf_path": record["source_pdf_path"],
        "target_pdf_path": record["target_pdf_path"],
        "target_html_path": record["target_html_path"],
        "sha256": record["pdf"]["sha256"],
        "size": record["pdf"]["size"],
        "page_count": record["pdf"]["page_count"],
        "routing_rule_ids_json": json.dumps(record["routing"]["rule_ids"], ensure_ascii=False),
        "vlm_matches_json": json.dumps(record["existing_vlm_matches"], ensure_ascii=False),
        "warnings_json": json.dumps(record["warnings"], ensure_ascii=False),
    }


def _copy_csv_row(action: dict[str, Any]) -> dict[str, Any]:
    row = {
        field: action.get(field)
        for field in COPY_CSV_FIELDS
        if field
        not in {"errors_json", "identity_decision_json", "routing_decision_json"}
    }
    row["identity_decision_json"] = json.dumps(
        action.get("identity_decision"), ensure_ascii=False
    )
    row["routing_decision_json"] = json.dumps(
        action.get("routing_decision"), ensure_ascii=False
    )
    row["errors_json"] = json.dumps(action.get("errors", []), ensure_ascii=False)
    return row


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _write_csv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _md_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _copy_plan_markdown(result: BuildResult) -> str:
    report = result.report
    lines = [
        "# 2026-08-31 顶会论文批次复制计划",
        "",
        f"- 模式：`{report['mode']}`",
        f"- 状态：`{report['status']}`",
        f"- 规范记录：{report['record_count']} / {report['expected_count']}",
        f"- 阻断错误：{len(report['errors'])}",
        f"- 未决身份 hold：{report.get('identity_decisions', {}).get('unresolved_holds', 0)}",
        f"- 未决路由 needs-review："
        f"{report.get('routing_decisions', {}).get('unresolved_needs_review', 0)}",
        f"- 作者解析：{report.get('author_resolution', {}).get('matched', 0)} / "
        f"{report.get('record_count', 0)}",
        "",
        "## 分组计数",
        "",
        "| 分组 | 数量 |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {_md_escape(group)} | {count} |"
        for group, count in report["source_group_counts"].items()
    )
    lines.extend(
        [
            "",
            "## 复制动作",
            "",
            "| paper_id | 分组 | 领域 | 计划动作 | 应用状态 | 源 | 目标 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for action in result.copy_actions:
        lines.append(
            "| "
            + " | ".join(
                _md_escape(action.get(key))
                for key in (
                    "paper_id",
                    "source_group",
                    "domain",
                    "planned_action",
                    "apply_status",
                    "source_path",
                    "target_path",
                )
            )
            + " |"
        )
    lines.extend(["", "## 阻断错误", ""])
    if report["errors"]:
        lines.extend(f"- `{_md_escape(error)}`" for error in report["errors"])
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "> 本计划仅复制 PDF；HTML、代码、Hub 旁车和关系由后续独立门禁处理。",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(result: BuildResult, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "report": output_dir / "build-report.json",
        "paper_records_json": output_dir / "paper-records.json",
        "paper_records_jsonl": output_dir / "paper-records.jsonl",
        "paper_records_csv": output_dir / "paper-records.csv",
        "copy_plan_json": output_dir / "copy-plan.json",
        "copy_plan_csv": output_dir / "copy-plan.csv",
        "copy_plan_md": output_dir / "copy-plan.md",
    }
    _atomic_text(
        files["report"], json.dumps(result.report, ensure_ascii=False, indent=2) + "\n"
    )
    _atomic_text(
        files["paper_records_json"],
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "records": result.records},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_text(
        files["paper_records_jsonl"],
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in result.records),
    )
    _write_csv(
        files["paper_records_csv"], PAPER_CSV_FIELDS, map(_paper_csv_row, result.records)
    )
    _atomic_text(
        files["copy_plan_json"],
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": result.report["mode"],
                "status": result.report["status"],
                "actions": result.copy_actions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _write_csv(files["copy_plan_csv"], COPY_CSV_FIELDS, map(_copy_csv_row, result.copy_actions))
    _atomic_text(files["copy_plan_md"], _copy_plan_markdown(result))
    return {name: str(path.resolve()) for name, path in files.items()}
