from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pypdf import PdfReader

TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = "paper-analysis-evidence.v1"
CHECKPOINT_SCHEMA_VERSION = "paper-analysis-evidence.checkpoint.v1"

CATEGORY_KEYS = (
    "abstract",
    "introduction_contributions",
    "method",
    "experiments_results",
    "limitations_conclusion",
    "code_urls",
)
TEXT_CATEGORY_KEYS = CATEGORY_KEYS[:-1]

DEFAULT_MAX_EVIDENCE_CHARS = 18_000
DEFAULT_MAX_EXCERPT_CHARS = 1_200
DEFAULT_MAX_ITEMS_PER_CATEGORY = 3
MIN_EXCERPT_CHARS = 40

PAPER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
RESULT_SIGNAL_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*%|\b(?:outperform|improv|achiev|reduc|increase|decrease)\w*\b|"
    r"\b(?:accuracy|latency|memory|throughput|success rate|score|fps|flops?)\b)",
    re.IGNORECASE,
)
CONTRIBUTION_SIGNAL_RE = re.compile(
    r"(?:\bcontribut\w*\b|\bwe\s+(?:propose|present|introduce|develop|design)\b)",
    re.IGNORECASE,
)
CODE_CONTEXT_RE = re.compile(
    r"\b(?:code|source|implementation|repository|project\s+page|github|gitlab)\b",
    re.IGNORECASE,
)

REPOSITORY_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
NON_CODE_HOSTS = {
    "arxiv.org",
    "doi.org",
    "dx.doi.org",
    "openreview.net",
    "aclanthology.org",
}


class EvidenceError(ValueError):
    """A record cannot safely produce a page-located evidence package."""


@dataclass(frozen=True)
class SourceSegment:
    category: str
    section: str
    page: int | None
    text: str
    source_kind: str
    locator: dict[str, Any]
    page_mapping: str | None = None


@dataclass
class PdfContent:
    pages: list[str]
    annotation_urls: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


def _safe_text(value: object) -> str:
    text = str(value)
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_paper_records(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"无法读取 paper-record JSON：{exc}") from exc

    records: object
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        wrapped = next(
            (
                payload[key]
                for key in ("papers", "items", "records")
                if key in payload and isinstance(payload[key], list)
            ),
            None,
        )
        records = wrapped if wrapped is not None else [payload]
    else:
        raise EvidenceError("paper-record JSON 必须是对象、数组或 papers/items/records 包装对象。")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise EvidenceError(f"第 {index + 1} 条记录不是 JSON 对象。")
        paper_id = record.get("paper_id") or record.get("id")
        if not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id):
            raise EvidenceError(
                f"第 {index + 1} 条记录缺少合法 paper_id/id；要求小写字母、数字和连字符。"
            )
        if paper_id in seen:
            raise EvidenceError(f"批量输入包含重复 paper_id：{paper_id}")
        seen.add(paper_id)
        copied = dict(record)
        copied["paper_id"] = paper_id
        normalized.append(copied)
    if not normalized:
        raise EvidenceError("paper-record JSON 没有记录。")
    return normalized


def _nested_path(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def _record_pdf_values(record: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("target_pdf_path", "source_pdf_path"):
        value = _nested_path(record.get(key))
        if value and value not in values:
            values.append(value)
    pdf_value = _nested_path(record.get("pdf"))
    if pdf_value and pdf_value not in values:
        values.append(pdf_value)
    legacy_pdf_value = _nested_path(record.get("pdf_path"))
    if legacy_pdf_value and legacy_pdf_value not in values:
        values.append(legacy_pdf_value)
    representations = record.get("representations")
    if isinstance(representations, dict):
        representation_value = _nested_path(representations.get("pdf"))
        if representation_value and representation_value not in values:
            values.append(representation_value)
    return values


def _record_mineru_value(record: Mapping[str, Any]) -> str | None:
    for key in ("mineru_markdown_path", "mineru_markdown"):
        value = _nested_path(record.get(key))
        if value:
            return value
    representations = record.get("representations")
    if isinstance(representations, dict):
        return _nested_path(representations.get("mineru_markdown"))
    return None


def _resolve_input_path(raw_path: str, base_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError(f"输入文件不存在或不可访问：{candidate}（{exc}）") from exc
    if not resolved.is_file():
        raise EvidenceError(f"输入不是常规文件：{candidate}")
    return resolved


def _resolve_first_existing_pdf(raw_paths: Sequence[str], base_dir: Path) -> Path:
    attempted: list[str] = []
    for raw_path in raw_paths:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        attempted.append(str(candidate))
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    if not attempted:
        raise EvidenceError(
            "记录缺少 PDF 路径（target_pdf_path、source_pdf_path、pdf.path、pdf_path "
            "或 representations.pdf.path）。"
        )
    raise EvidenceError("没有可访问的 PDF；按优先级尝试：" + " | ".join(attempted))


def _record_paths(
    record: Mapping[str, Any],
    base_dir: Path,
    *,
    pdf_override: Path | None = None,
    mineru_override: Path | None = None,
) -> tuple[Path, Path | None]:
    if pdf_override:
        pdf_path = _resolve_input_path(str(pdf_override), base_dir)
    else:
        pdf_path = _resolve_first_existing_pdf(_record_pdf_values(record), base_dir)
    if pdf_path.suffix.lower() != ".pdf":
        raise EvidenceError(f"PDF 输入扩展名不是 .pdf：{pdf_path}")
    with pdf_path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise EvidenceError(f"文件没有 PDF 魔数：{pdf_path}")

    raw_markdown = str(mineru_override) if mineru_override else _record_mineru_value(record)
    markdown_path = _resolve_input_path(raw_markdown, base_dir) if raw_markdown else None
    if markdown_path and markdown_path.suffix.lower() not in {".md", ".markdown"}:
        raise EvidenceError(f"MinerU 输入扩展名不是 .md/.markdown：{markdown_path}")
    return pdf_path, markdown_path


def _provided_pdf_sha(record: Mapping[str, Any]) -> str | None:
    pdf = record.get("pdf")
    if isinstance(pdf, dict):
        value = pdf.get("sha256")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()):
            return value.strip().lower()
    return None


def _fingerprint_inputs(
    record: Mapping[str, Any],
    pdf_path: Path,
    markdown_path: Path | None,
    *,
    max_evidence_chars: int,
    max_excerpt_chars: int,
    max_items_per_category: int,
) -> tuple[str, dict[str, Any]]:
    pdf_sha256 = sha256_file(pdf_path)
    declared_sha = _provided_pdf_sha(record)
    if declared_sha and declared_sha != pdf_sha256:
        raise EvidenceError(
            "paper-record 中的 PDF SHA-256 与实际文件不一致："
            f"声明 {declared_sha}，实际 {pdf_sha256}"
        )
    markdown_sha256 = sha256_file(markdown_path) if markdown_path else None
    record_sha256 = _sha256_bytes(_canonical_json(record))
    fingerprint_payload = {
        "tool_version": TOOL_VERSION,
        "record_sha256": record_sha256,
        "pdf_sha256": pdf_sha256,
        "mineru_markdown_sha256": markdown_sha256,
        "options": {
            "max_evidence_chars": max_evidence_chars,
            "max_excerpt_chars": max_excerpt_chars,
            "max_items_per_category": max_items_per_category,
        },
    }
    return _sha256_bytes(_canonical_json(fingerprint_payload)), fingerprint_payload


def _clean_page_text(text: object) -> str:
    return _safe_text(text).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _annotation_uri(annotation: object) -> str | None:
    try:
        annotation_object = annotation.get_object()  # type: ignore[attr-defined]
    except AttributeError:
        annotation_object = annotation
    if not isinstance(annotation_object, Mapping):
        return None
    action = annotation_object.get("/A")
    try:
        action = action.get_object()  # type: ignore[union-attr]
    except AttributeError:
        pass
    if not isinstance(action, Mapping) or action.get("/S") != "/URI":
        return None
    uri = action.get("/URI")
    return _safe_text(uri).strip() if uri else None


def extract_pdf_content(path: Path) -> PdfContent:
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        raise EvidenceError(f"无法解析 PDF：{type(exc).__name__}: {exc}") from exc
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise EvidenceError("PDF 已加密且不能用空密码读取。")
        except EvidenceError:
            raise
        except Exception as exc:
            raise EvidenceError(f"无法解密 PDF：{type(exc).__name__}: {exc}") from exc

    pages: list[str] = []
    annotation_urls: list[dict[str, Any]] = []
    warnings: list[str] = []
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            pages.append(_clean_page_text(page.extract_text() or ""))
        except Exception as exc:
            pages.append("")
            warnings.append(
                f"pdf_page_text_error:page={page_index}:{type(exc).__name__}:{_safe_text(exc)}"
            )
        try:
            annotations = page.get("/Annots") or []
            for annotation_index, annotation in enumerate(annotations):
                uri = _annotation_uri(annotation)
                if uri:
                    annotation_urls.append(
                        {
                            "page": page_index,
                            "annotation_index": annotation_index,
                            "uri": uri,
                        }
                    )
        except Exception as exc:
            warnings.append(
                f"pdf_annotation_error:page={page_index}:{type(exc).__name__}:{_safe_text(exc)}"
            )
    if not pages:
        raise EvidenceError("PDF 没有页面。")
    return PdfContent(pages=pages, annotation_urls=annotation_urls, warnings=warnings)


def _strip_heading_number(value: str) -> str:
    stripped = value.strip().strip("#").strip()
    stripped = re.sub(
        r"^(?:(?:section|chapter)\s+)?(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)\s:-]+",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    return stripped.strip().rstrip(":：").strip()


def _heading_category(value: str) -> str | None:
    if not value.strip() or len(value.strip()) > 140:
        return None
    normalized = _strip_heading_number(value)
    if not normalized or len(normalized.split()) > 16:
        return None
    lower = normalized.casefold()
    if re.match(
        r"^(?:references|bibliography|acknowledg(?:e)?ments?|appendix|supplementary)\b",
        lower,
    ):
        return "__stop__"
    if re.fullmatch(r"(?:abstract|summary|摘要)", lower):
        return "abstract"
    if re.match(
        r"^(?:introduction|motivation|overview|contributions?|our\s+contributions?)\b",
        lower,
    ) or lower.startswith(("引言", "介绍", "贡献")):
        return "introduction_contributions"
    if re.match(
        r"^(?:(?:our|proposed)\s+)?(?:method(?:s|ology)?|approach|framework|model|"
        r"architecture|system\s+design|technical\s+approach)\b",
        lower,
    ) or lower.startswith(("方法", "模型", "框架", "架构")):
        return "method"
    if re.match(
        r"^(?:experiment(?:s|al)?|evaluation|results?|ablation|empirical\s+evaluation)\b",
        lower,
    ) or lower.startswith(("实验", "评测", "结果", "消融")):
        return "experiments_results"
    if re.search(
        r"\b(?:limitations?|discussion|conclusions?|future\s+work)\b",
        lower,
    ) or any(word in lower for word in ("局限", "讨论", "结论", "未来工作")):
        return "limitations_conclusion"
    return None


def _is_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return False
    if stripped.endswith((".", "。", ";", "；", ",", "，")):
        return False
    if _heading_category(stripped) is not None:
        return True
    if re.match(r"^(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)\s:-]+\S+", stripped, re.IGNORECASE):
        return len(_strip_heading_number(stripped).split()) <= 14
    return stripped.isupper() and 1 <= len(stripped.split()) <= 12


def pdf_segments(pages: Sequence[str]) -> list[SourceSegment]:
    segments: list[SourceSegment] = []
    current_category: str | None = None
    current_section = ""

    def flush(
        category: str | None,
        section: str,
        page: int,
        start_line: int,
        end_line: int,
        lines_to_flush: Sequence[str],
    ) -> None:
        text = "\n".join(lines_to_flush).strip()
        if category not in TEXT_CATEGORY_KEYS or not text:
            return
        segments.append(
            SourceSegment(
                category=category,
                section=section or category,
                page=page,
                text=text,
                source_kind="pdf_text",
                locator={
                    "pdf_page": page,
                    "page_line_start": start_line,
                    "page_line_end": end_line,
                },
                page_mapping="native_pdf_page",
            )
        )

    for page_number, page_text in enumerate(pages, start=1):
        content_lines: list[str] = []
        segment_start_line = 1

        lines = page_text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            if _is_heading_line(line):
                flush(
                    current_category,
                    current_section,
                    page_number,
                    segment_start_line,
                    line_number - 1,
                    content_lines,
                )
                content_lines = []
                category = _heading_category(line)
                current_category = None if category == "__stop__" else category
                current_section = line.strip()
                segment_start_line = line_number + 1
                continue
            if current_category in TEXT_CATEGORY_KEYS:
                content_lines.append(line)
        flush(
            current_category,
            current_section,
            page_number,
            segment_start_line,
            len(lines),
            content_lines,
        )
    return segments


PAGE_MARKER_PATTERNS = (
    re.compile(
        r"^\s*<!--\s*(?:page|page[-_ ]?number)\s*[:=]?\s*[\"']?(\d+)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*<!--\s*page[-_ ]?(?:idx|index)\s*[:=]\s*[\"']?(\d+)", re.IGNORECASE),
    re.compile(r"^\s*(?:#{1,6}\s*)?(?:page|页)\s*[:：]?\s*(\d+)\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*page\s+(\d+)\s*-{2,}\s*$", re.IGNORECASE),
)


def _markdown_page_marker(line: str) -> tuple[int, str] | None:
    for index, pattern in enumerate(PAGE_MARKER_PATTERNS):
        match = pattern.search(line)
        if match:
            value = int(match.group(1))
            if index == 1:
                value += 1
            return value, "mineru_page_marker"
    return None


def _markdown_heading(line: str) -> str | None:
    match = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", line)
    if match:
        return match.group(1).strip()
    stripped = line.strip()
    return stripped if _is_heading_line(stripped) else None


def markdown_segments(path: Path) -> list[SourceSegment]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise EvidenceError(f"无法读取 MinerU Markdown：{exc}") from exc
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[SourceSegment] = []
    current_category: str | None = None
    current_section = ""
    current_page: int | None = None
    current_mapping: str | None = None
    content_lines: list[str] = []
    segment_start_line = 1

    def flush(end_line: int) -> None:
        nonlocal content_lines
        content = "\n".join(content_lines).strip()
        if current_category in TEXT_CATEGORY_KEYS and content:
            segments.append(
                SourceSegment(
                    category=current_category,
                    section=current_section or current_category,
                    page=current_page,
                    text=content,
                    source_kind="mineru_markdown",
                    locator={
                        "markdown_path": str(path),
                        "markdown_line_start": segment_start_line,
                        "markdown_line_end": end_line,
                    },
                    page_mapping=current_mapping,
                )
            )
        content_lines = []

    for line_number, line in enumerate(lines, start=1):
        marker = _markdown_page_marker(line)
        if marker:
            flush(line_number - 1)
            current_page, current_mapping = marker
            segment_start_line = line_number + 1
            continue
        heading = _markdown_heading(line)
        if heading is not None:
            flush(line_number - 1)
            category = _heading_category(heading)
            current_category = None if category == "__stop__" else category
            current_section = heading
            segment_start_line = line_number + 1
            continue
        if current_category in TEXT_CATEGORY_KEYS:
            if re.fullmatch(r"\s*!\[[^]]*]\([^)]*\)\s*", line):
                continue
            content_lines.append(line)
    flush(len(lines))
    return segments


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE).strip()


def _tokens_for_match(text: str) -> set[str]:
    return {token for token in _normalize_for_match(text).split() if len(token) >= 3}


def _map_markdown_page(segment: SourceSegment, pages: Sequence[str]) -> SourceSegment | None:
    if segment.page is not None:
        if 1 <= segment.page <= len(pages):
            return segment
        return None

    normalized_excerpt = _normalize_for_match(segment.text)[:500]
    normalized_pages = [_normalize_for_match(page) for page in pages]
    if len(normalized_excerpt) >= 80:
        exact_pages = [
            index + 1
            for index, page in enumerate(normalized_pages)
            if normalized_excerpt[:200] in page
        ]
        if len(exact_pages) == 1:
            return SourceSegment(
                **{
                    **segment.__dict__,
                    "page": exact_pages[0],
                    "page_mapping": "pdf_exact_text_match",
                }
            )

    query_tokens = _tokens_for_match(segment.text[:2_000])
    if len(query_tokens) < 8:
        return None
    scores: list[tuple[float, int]] = []
    for page_number, page in enumerate(pages, start=1):
        page_tokens = _tokens_for_match(page)
        score = len(query_tokens & page_tokens) / len(query_tokens) if page_tokens else 0.0
        scores.append((score, page_number))
    scores.sort(reverse=True)
    best_score, best_page = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    if best_score >= 0.55 and best_score - second_score >= 0.08:
        return SourceSegment(
            **{
                **segment.__dict__,
                "page": best_page,
                "page_mapping": f"pdf_token_overlap:{best_score:.3f}",
            }
        )
    return None


def _normalize_excerpt(text: str) -> str:
    without_markers = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)

    def sanitize_match(match: re.Match[str]) -> str:
        return _sanitize_url(match.group(0)) or "[invalid-url-redacted]"

    sanitized = URL_RE.sub(sanitize_match, without_markers)
    return re.sub(r"\s+", " ", sanitized).strip()


def _truncate_excerpt(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    truncated = text[:limit]
    last_space = truncated.rfind(" ")
    if last_space >= max(MIN_EXCERPT_CHARS, int(limit * 0.75)):
        truncated = truncated[:last_space]
    return truncated.rstrip(), True


def _candidate_excerpts(segment: SourceSegment) -> list[tuple[str, str]]:
    normalized = _normalize_excerpt(segment.text)
    if not normalized:
        return []
    candidates: list[tuple[str, str]] = [(normalized, "section_opening")]
    sentences = [sentence.strip() for sentence in SENTENCE_SPLIT_RE.split(normalized)]
    if segment.category == "introduction_contributions":
        candidates.extend(
            (sentence, "contribution_statement")
            for sentence in sentences
            if CONTRIBUTION_SIGNAL_RE.search(sentence)
        )
    elif segment.category == "experiments_results":
        result_sentences = [sentence for sentence in sentences if RESULT_SIGNAL_RE.search(sentence)]
        if result_sentences:
            candidates = [(sentence, "result_statement") for sentence in result_sentences]
    return candidates


def _sanitize_url(raw_url: str) -> str | None:
    candidate = raw_url.strip().rstrip(".,;:!?)]}>'\"")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def _code_url_kind(url: str, context: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if host in REPOSITORY_HOSTS and len(parts) >= 2:
        return "repository"
    if host.endswith(".github.io") or host.endswith(".gitlab.io"):
        return "project_page"
    if host in NON_CODE_HOSTS:
        return None
    if CODE_CONTEXT_RE.search(context):
        return "code_context_url"
    return None


def _url_context(text: str, start: int, end: int, sanitized_url: str) -> str:
    left = max(0, start - 180)
    right = min(len(text), end + 180)
    context = text[left:start] + sanitized_url + text[end:right]
    return _normalize_excerpt(context)


def _code_url_candidates(
    segments: Sequence[SourceSegment],
    annotations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for segment in segments:
        if segment.page is None:
            continue
        for match in URL_RE.finditer(segment.text):
            sanitized = _sanitize_url(match.group(0))
            if not sanitized:
                continue
            context = _url_context(segment.text, match.start(), match.end(), sanitized)
            kind = _code_url_kind(sanitized, context)
            if not kind or (sanitized, segment.page) in seen:
                continue
            seen.add((sanitized, segment.page))
            candidates.append(
                {
                    "url": sanitized,
                    "candidate_kind": kind,
                    "verification": "candidate_unverified",
                    "section": segment.section,
                    "page": segment.page,
                    "excerpt": context,
                    "source_kind": segment.source_kind,
                    "locator": dict(segment.locator),
                    "page_mapping": segment.page_mapping,
                    "selection_reason": "code_url_in_section_text",
                    "normalization": "whitespace_collapsed;url_userinfo_query_fragment_removed",
                }
            )
    for annotation in annotations:
        raw_url = _safe_text(annotation.get("uri", ""))
        sanitized = _sanitize_url(raw_url)
        page = annotation.get("page")
        if not sanitized or not isinstance(page, int):
            continue
        kind = _code_url_kind(sanitized, sanitized)
        if not kind or (sanitized, page) in seen:
            continue
        seen.add((sanitized, page))
        candidates.append(
            {
                "url": sanitized,
                "candidate_kind": kind,
                "verification": "candidate_unverified",
                "section": "PDF link annotation",
                "page": page,
                "excerpt": sanitized,
                "source_kind": "pdf_annotation",
                "locator": {
                    "pdf_page": page,
                    "annotation_index": annotation.get("annotation_index"),
                },
                "page_mapping": "native_pdf_page",
                "selection_reason": "code_url_in_pdf_annotation",
                "normalization": "url_userinfo_query_fragment_removed",
            }
        )
    return candidates


def _add_evidence_item(
    bucket: list[dict[str, Any]],
    candidate: Mapping[str, Any],
    *,
    evidence_number: int,
    remaining_chars: int,
    max_excerpt_chars: int,
) -> tuple[int, int]:
    raw_excerpt = _normalize_excerpt(_safe_text(candidate.get("excerpt", "")))
    allowed = min(remaining_chars, max_excerpt_chars)
    minimum = 8 if candidate.get("url") else MIN_EXCERPT_CHARS
    if allowed < minimum or len(raw_excerpt) < minimum:
        return evidence_number, 0
    excerpt, truncated = _truncate_excerpt(raw_excerpt, allowed)
    if len(excerpt) < minimum:
        return evidence_number, 0
    item = dict(candidate)
    item["evidence_id"] = f"ev-{evidence_number:04d}"
    item["excerpt"] = excerpt
    item["excerpt_sha256"] = _sha256_bytes(excerpt.encode("utf-8"))
    item["truncated"] = truncated
    item.setdefault("normalization", "whitespace_collapsed")
    bucket.append(item)
    return evidence_number + 1, len(excerpt)


def build_evidence(
    pdf_content: PdfContent,
    markdown_path: Path | None,
    *,
    max_evidence_chars: int,
    max_excerpt_chars: int,
    max_items_per_category: int,
) -> tuple[dict[str, list[dict[str, Any]]], list[str], int]:
    warnings = list(pdf_content.warnings)
    pdf_source_segments = pdf_segments(pdf_content.pages)
    markdown_source_segments: list[SourceSegment] = []
    if markdown_path:
        for segment in markdown_segments(markdown_path):
            mapped = _map_markdown_page(segment, pdf_content.pages)
            if mapped is None:
                warnings.append(
                    "mineru_segment_page_unresolved:"
                    f"section={_safe_text(segment.section)[:120]}:"
                    f"lines={segment.locator.get('markdown_line_start')}-"
                    f"{segment.locator.get('markdown_line_end')}"
                )
            else:
                markdown_source_segments.append(mapped)

    evidence: dict[str, list[dict[str, Any]]] = {key: [] for key in CATEGORY_KEYS}
    evidence_number = 1
    used_chars = 0
    seen_excerpts: set[str] = set()

    for category in TEXT_CATEGORY_KEYS:
        category_limit = 1 if category == "abstract" else max_items_per_category
        ordered_segments = [
            segment
            for segment in (*markdown_source_segments, *pdf_source_segments)
            if segment.category == category and segment.page is not None
        ]
        for segment in ordered_segments:
            for excerpt, selection_reason in _candidate_excerpts(segment):
                dedupe_key = _normalize_for_match(excerpt)[:500]
                if not dedupe_key or dedupe_key in seen_excerpts:
                    continue
                candidate = {
                    "section": segment.section,
                    "page": segment.page,
                    "excerpt": excerpt,
                    "source_kind": segment.source_kind,
                    "locator": dict(segment.locator),
                    "page_mapping": segment.page_mapping,
                    "selection_reason": selection_reason,
                    "normalization": "whitespace_collapsed",
                }
                evidence_number, added = _add_evidence_item(
                    evidence[category],
                    candidate,
                    evidence_number=evidence_number,
                    remaining_chars=max_evidence_chars - used_chars,
                    max_excerpt_chars=max_excerpt_chars,
                )
                if added:
                    seen_excerpts.add(dedupe_key)
                    used_chars += added
                if len(evidence[category]) >= category_limit or used_chars >= max_evidence_chars:
                    break
            if len(evidence[category]) >= category_limit or used_chars >= max_evidence_chars:
                break

    all_segments = [*markdown_source_segments, *pdf_source_segments]
    for candidate in _code_url_candidates(all_segments, pdf_content.annotation_urls):
        if len(evidence["code_urls"]) >= max_items_per_category:
            break
        evidence_number, added = _add_evidence_item(
            evidence["code_urls"],
            candidate,
            evidence_number=evidence_number,
            remaining_chars=max_evidence_chars - used_chars,
            max_excerpt_chars=max_excerpt_chars,
        )
        used_chars += added
        if used_chars >= max_evidence_chars:
            break

    return evidence, sorted(set(warnings)), used_chars


def _record_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "paper_id": record["paper_id"],
        "title": record.get("title"),
        "title_zh": record.get("title_zh"),
        "year": record.get("year"),
        "venue": record.get("venue"),
    }


def extract_record(
    record: Mapping[str, Any],
    *,
    base_dir: Path,
    max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    max_items_per_category: int = DEFAULT_MAX_ITEMS_PER_CATEGORY,
    pdf_override: Path | None = None,
    mineru_override: Path | None = None,
) -> tuple[dict[str, Any], str]:
    if max_evidence_chars < 1_000:
        raise EvidenceError("max_evidence_chars 必须至少为 1000。")
    if max_excerpt_chars < MIN_EXCERPT_CHARS:
        raise EvidenceError(f"max_excerpt_chars 必须至少为 {MIN_EXCERPT_CHARS}。")
    if max_items_per_category < 1:
        raise EvidenceError("max_items_per_category 必须至少为 1。")

    pdf_path, markdown_path = _record_paths(
        record,
        base_dir,
        pdf_override=pdf_override,
        mineru_override=mineru_override,
    )
    fingerprint, source_fingerprints = _fingerprint_inputs(
        record,
        pdf_path,
        markdown_path,
        max_evidence_chars=max_evidence_chars,
        max_excerpt_chars=max_excerpt_chars,
        max_items_per_category=max_items_per_category,
    )
    pdf_before = pdf_path.stat()
    markdown_before = markdown_path.stat() if markdown_path else None
    pdf_content = extract_pdf_content(pdf_path)
    evidence, warnings, used_chars = build_evidence(
        pdf_content,
        markdown_path,
        max_evidence_chars=max_evidence_chars,
        max_excerpt_chars=max_excerpt_chars,
        max_items_per_category=max_items_per_category,
    )
    pdf_after = pdf_path.stat()
    markdown_after = markdown_path.stat() if markdown_path else None
    if (pdf_before.st_size, pdf_before.st_mtime_ns) != (
        pdf_after.st_size,
        pdf_after.st_mtime_ns,
    ) or (
        markdown_path
        and markdown_before
        and markdown_after
        and (markdown_before.st_size, markdown_before.st_mtime_ns)
        != (markdown_after.st_size, markdown_after.st_mtime_ns)
    ):
        raise EvidenceError("输入文件在抽取期间发生变化，已拒绝生成不一致证据。")

    primary_count = sum(len(evidence[key]) for key in TEXT_CATEGORY_KEYS)
    if primary_count == 0:
        raise EvidenceError(
            "未提取到带 PDF 页码的正文证据；若 PDF 为扫描件，请提供含页码标记的 MinerU Markdown。"
        )
    missing_categories = [key for key in TEXT_CATEGORY_KEYS if not evidence[key]]
    if missing_categories:
        warnings.append("missing_evidence_categories:" + ",".join(missing_categories))

    result = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "status": "ok",
        "paper": _record_identity(record),
        "policy": {
            "verbatim_evidence_only": True,
            "generated_summary_present": False,
            "every_evidence_item_requires_pdf_page": True,
            "max_evidence_chars": max_evidence_chars,
            "max_excerpt_chars": max_excerpt_chars,
            "max_items_per_category": max_items_per_category,
        },
        "sources": {
            "paper_record_sha256": source_fingerprints["record_sha256"],
            "pdf": {
                "path": str(pdf_path),
                "sha256": source_fingerprints["pdf_sha256"],
                "page_count": len(pdf_content.pages),
            },
            "mineru_markdown": (
                {
                    "path": str(markdown_path),
                    "sha256": source_fingerprints["mineru_markdown_sha256"],
                }
                if markdown_path
                else None
            ),
        },
        "evidence": evidence,
        "coverage": {key: len(value) for key, value in evidence.items()},
        "missing_categories": missing_categories,
        "total_evidence_chars": used_chars,
        "warnings": sorted(set(warnings)),
    }
    return result, fingerprint


def _empty_checkpoint() -> dict[str, Any]:
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "records": {},
    }


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_checkpoint()
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"断点文件损坏，拒绝静默忽略：{path}（{exc}）") from exc
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
    ):
        raise EvidenceError(f"断点文件 schema 不兼容：{path}")
    records = checkpoint.get("records")
    if not isinstance(records, dict):
        raise EvidenceError(f"断点文件 records 不是对象：{path}")
    return checkpoint


def _error_payload(
    record: Mapping[str, Any],
    exc: Exception,
    *,
    max_evidence_chars: int,
    max_excerpt_chars: int,
    max_items_per_category: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "status": "error",
        "paper": _record_identity(record),
        "policy": {
            "verbatim_evidence_only": True,
            "generated_summary_present": False,
            "every_evidence_item_requires_pdf_page": True,
            "max_evidence_chars": max_evidence_chars,
            "max_excerpt_chars": max_excerpt_chars,
            "max_items_per_category": max_items_per_category,
        },
        "evidence": {key: [] for key in CATEGORY_KEYS},
        "coverage": {key: 0 for key in CATEGORY_KEYS},
        "missing_categories": list(TEXT_CATEGORY_KEYS),
        "total_evidence_chars": 0,
        "warnings": [],
        "error": {
            "type": type(exc).__name__,
            "message": _safe_text(exc),
        },
    }


def run_batch(
    records: Sequence[dict[str, Any]],
    *,
    base_dir: Path,
    output_dir: Path,
    checkpoint_path: Path | None = None,
    report_path: Path | None = None,
    max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    max_items_per_category: int = DEFAULT_MAX_ITEMS_PER_CATEGORY,
    force: bool = False,
    pdf_override: Path | None = None,
    mineru_override: Path | None = None,
) -> dict[str, Any]:
    if (pdf_override or mineru_override) and len(records) != 1:
        raise EvidenceError("--pdf/--mineru-markdown 覆盖参数仅允许单记录输入。")
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_path or output_dir / "checkpoint.json"
    report_path = report_path or output_dir / "batch-report.json"
    checkpoint = _load_checkpoint(checkpoint_path)
    checkpoint_records: dict[str, Any] = checkpoint["records"]

    results: list[dict[str, Any]] = []
    processed = skipped = succeeded = failed = 0
    for record in records:
        paper_id = record["paper_id"]
        output_path = output_dir / f"{paper_id}.evidence.json"
        fingerprint: str | None = None
        try:
            pdf_path, markdown_path = _record_paths(
                record,
                base_dir,
                pdf_override=pdf_override,
                mineru_override=mineru_override,
            )
            fingerprint, _ = _fingerprint_inputs(
                record,
                pdf_path,
                markdown_path,
                max_evidence_chars=max_evidence_chars,
                max_excerpt_chars=max_excerpt_chars,
                max_items_per_category=max_items_per_category,
            )
            previous = checkpoint_records.get(paper_id)
            if (
                not force
                and isinstance(previous, dict)
                and previous.get("status") == "ok"
                and previous.get("fingerprint") == fingerprint
                and output_path.is_file()
                and previous.get("output_sha256") == sha256_file(output_path)
            ):
                skipped += 1
                succeeded += 1
                results.append(
                    {
                        "paper_id": paper_id,
                        "status": "ok",
                        "action": "skipped_unchanged",
                        "output": str(output_path),
                    }
                )
                continue

            payload, confirmed_fingerprint = extract_record(
                record,
                base_dir=base_dir,
                max_evidence_chars=max_evidence_chars,
                max_excerpt_chars=max_excerpt_chars,
                max_items_per_category=max_items_per_category,
                pdf_override=pdf_override,
                mineru_override=mineru_override,
            )
            if confirmed_fingerprint != fingerprint:
                raise EvidenceError("抽取前后的输入指纹不一致。")
            _atomic_write_json(output_path, payload)
            output_sha256 = sha256_file(output_path)
            checkpoint_records[paper_id] = {
                "status": "ok",
                "fingerprint": fingerprint,
                "output": output_path.name,
                "output_sha256": output_sha256,
            }
            processed += 1
            succeeded += 1
            results.append(
                {
                    "paper_id": paper_id,
                    "status": "ok",
                    "action": "processed",
                    "output": str(output_path),
                    "coverage": payload["coverage"],
                    "total_evidence_chars": payload["total_evidence_chars"],
                }
            )
        except Exception as exc:
            payload = _error_payload(
                record,
                exc,
                max_evidence_chars=max_evidence_chars,
                max_excerpt_chars=max_excerpt_chars,
                max_items_per_category=max_items_per_category,
            )
            _atomic_write_json(output_path, payload)
            output_sha256 = sha256_file(output_path)
            checkpoint_records[paper_id] = {
                "status": "error",
                "fingerprint": fingerprint,
                "output": output_path.name,
                "output_sha256": output_sha256,
                "error_type": type(exc).__name__,
            }
            processed += 1
            failed += 1
            results.append(
                {
                    "paper_id": paper_id,
                    "status": "error",
                    "action": "processed",
                    "output": str(output_path),
                    "error": payload["error"],
                }
            )
        finally:
            _atomic_write_json(checkpoint_path, checkpoint)

    report = {
        "schema_version": "paper-analysis-evidence.batch-report.v1",
        "tool_version": TOOL_VERSION,
        "status": "passed" if failed == 0 else "completed_with_errors",
        "record_count": len(records),
        "processed": processed,
        "skipped_unchanged": skipped,
        "succeeded": succeeded,
        "failed": failed,
        "output_dir": str(output_dir.resolve()),
        "checkpoint": str(checkpoint_path.resolve()),
        "results": results,
    }
    _atomic_write_json(report_path, report)
    return report


def validate_record_ids(records: Iterable[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for record in records:
        paper_id = record.get("paper_id")
        if not isinstance(paper_id, str) or not PAPER_ID_RE.fullmatch(paper_id):
            raise EvidenceError(f"非法 paper_id：{paper_id!r}")
        if paper_id in seen:
            raise EvidenceError(f"重复 paper_id：{paper_id}")
        seen.add(paper_id)
