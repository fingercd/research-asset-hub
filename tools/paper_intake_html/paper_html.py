from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import ParseResult, parse_qsl, unquote, urlparse

from pypdf import PdfReader

PENDING = "待核验"
PAPER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REMOTE_SCHEMES = {"http", "https"}
FORBIDDEN_SCHEMES = {"data", "file", "javascript", "vbscript"}
REQUIRED_SECTION_IDS = (
    "overview",
    "problem",
    "method",
    "results",
    "limitations",
    "relationships",
    "code",
    "sources",
    "verification",
)
CODE_STATUS_LABELS = {
    "official_local": "官方公开代码已本地归档",
    "official_url_only": "官方公开代码仅 URL，尚未本地归档",
    "announced_pending": "官方宣布但尚未公开",
    "none_found": "未发现可核验的公开官方代码",
    "third_party_only": "仅发现第三方实现",
    "not_applicable": "不适用",
    "unknown": PENDING,
}


class RecordError(ValueError):
    """Raised when a record cannot be rendered safely."""


@dataclass
class Issue:
    level: str
    code: str
    message: str
    path: str | None = None


@dataclass
class ValidationResult:
    path: str
    paper_id: str | None = None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    links_checked: int = 0
    verified_assets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data


def load_records(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise RecordError(f"输入不是严格 UTF-8：{path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RecordError(f"JSON 解析失败：{path}: {exc}") from exc

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for key in ("papers", "items", "records"):
            if key in payload:
                records = payload[key]
                break
        if records is None:
            records = [payload]
    else:
        raise RecordError(
            "输入顶层必须是记录对象、记录数组，或包含 papers/items/records 数组的对象。"
        )

    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise RecordError("记录集合必须是 JSON 对象数组。")
    if not records:
        raise RecordError("记录集合为空。")
    return [dict(item) for item in records]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    return None


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        result: list[str] = []
        for item in value:
            item_text = _text(item)
            if item_text:
                result.append(item_text)
        return result
    scalar_text = _text(value)
    return [scalar_text] if scalar_text else []


def _escape(value: Any) -> str:
    return html.escape(_text(value) or "", quote=True)


def _pending() -> str:
    return f'<span class="pending" data-status="pending">{PENDING}</span>'


def _value_or_pending(value: Any, *, css_class: str | None = None) -> str:
    text = _text(value)
    if not text:
        return _pending()
    class_attr = f' class="{html.escape(css_class, quote=True)}"' if css_class else ""
    return f"<span{class_attr}>{_escape(text)}</span>"


def _paragraphs_or_pending(value: Any) -> str:
    items = _text_list(value)
    if not items:
        return f"<p>{_pending()}</p>"
    return "".join(f"<p>{_escape(item)}</p>" for item in items)


def _list_or_pending(value: Any, *, css_class: str | None = None) -> str:
    items = _text_list(value)
    if not items:
        return f"<p>{_pending()}</p>"
    class_attr = f' class="{css_class}"' if css_class else ""
    return f"<ul{class_attr}>" + "".join(f"<li>{_escape(item)}</li>" for item in items) + "</ul>"


def _parse_url(value: str) -> ParseResult:
    try:
        return urlparse(value)
    except ValueError as exc:
        raise RecordError("链接语法无效。") from exc


def _fully_unquote(value: str) -> str:
    decoded = value
    for _ in range(32):
        next_value = unquote(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise RecordError("链接包含过度嵌套的百分号编码。")


def normalize_href(
    raw_value: Any,
    *,
    external_only: bool = False,
    local_only: bool = False,
) -> str | None:
    if external_only and local_only:
        raise RecordError("链接不能同时声明为 external_only 和 local_only。")
    value = _text(raw_value)
    if not value:
        return None
    value = value.replace("\\", "/")
    decoded = _fully_unquote(value)
    decoded_portable = decoded.replace("\\", "/")
    if any(ord(char) < 32 for char in decoded_portable):
        raise RecordError("链接包含控制字符。")
    parsed = _parse_url(value)
    decoded_parsed = _parse_url(decoded_portable)
    scheme = parsed.scheme.lower()
    decoded_scheme = decoded_parsed.scheme.lower()
    if scheme in FORBIDDEN_SCHEMES or decoded_scheme in FORBIDDEN_SCHEMES:
        raise RecordError(f"禁止的链接协议：{scheme or decoded_scheme}:")
    if decoded_scheme and not scheme:
        raise RecordError("链接协议不得使用百分号编码。")
    if local_only and (scheme or decoded_scheme or parsed.netloc or decoded_parsed.netloc):
        raise RecordError("本地表示路径不得填写远程 URL。")
    if scheme:
        if scheme not in REMOTE_SCHEMES or not parsed.netloc:
            raise RecordError("仅允许 http/https 外部链接。")
        if parsed.username or parsed.password:
            raise RecordError("外部链接不得包含 URL 用户名或密码。")
        sensitive_keys = {
            "access_token",
            "api_key",
            "apikey",
            "auth",
            "key",
            "sig",
            "signature",
            "token",
            "x-amz-signature",
        }
        if any(key.lower() in sensitive_keys for key, _ in parse_qsl(parsed.query)):
            raise RecordError("外部链接不得包含凭据或签名查询参数。")
        return value
    if external_only:
        raise RecordError("来源 URL 必须是 http/https。")
    if (
        value.startswith("/")
        or decoded_portable.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or re.match(r"^[A-Za-z]:", decoded_portable)
    ):
        raise RecordError(f"HTML 中只允许可移植的相对本地链接：{value}")
    return value


def _anchor(
    label: str,
    href: str,
    kind: str,
    *,
    data: Mapping[str, Any] | None = None,
) -> str:
    data_attributes = "".join(
        f' data-{_escape(key)}="{_escape(value)}"'
        for key, value in (data or {}).items()
        if _text(value)
    )
    return (
        f'<a data-link-kind="{_escape(kind)}"{data_attributes} href="{_escape(href)}"'
        + (
            ' target="_blank" rel="noopener noreferrer"'
            if _parse_url(href).scheme in REMOTE_SCHEMES
            else ""
        )
        + f">{_escape(label)}</a>"
    )


def _section(section_id: str, eyebrow: str, heading: str, content: str) -> str:
    return (
        f'<section id="{section_id}" class="paper-section" data-paper-section="{section_id}">'
        f'<div class="eyebrow">{_escape(eyebrow)}</div>'
        f"<h2>{_escape(heading)}</h2>{content}</section>"
    )


def validate_record(record: Mapping[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    paper_id = _text(record.get("paper_id"))
    if not paper_id:
        issues.append(Issue("error", "paper_id_missing", "缺少稳定 paper_id。"))
    elif not PAPER_ID_RE.fullmatch(paper_id):
        issues.append(
            Issue(
                "error",
                "paper_id_invalid",
                "paper_id 必须由小写英文、数字和连字符组成，长度 2–128。",
            )
        )

    for field_name in ("title", "title_zh", "authors", "year", "venue"):
        value = record.get(field_name)
        if field_name == "authors":
            missing = not _text_list(value)
        else:
            missing = not _text(value)
        if missing:
            issues.append(Issue("warning", "metadata_pending", f"{field_name} 待核验。"))

    for field_name in ("title", "title_zh", "venue", "doi", "arxiv_id", "abstract", "summary"):
        value = record.get(field_name)
        if value is not None and not isinstance(value, str):
            issues.append(
                Issue("error", "field_type_invalid", f"{field_name} 必须是字符串或 null。")
            )
    authors_value = record.get("authors")
    if authors_value is not None and not (
        isinstance(authors_value, str)
        or (
            isinstance(authors_value, list) and all(isinstance(item, str) for item in authors_value)
        )
    ):
        issues.append(
            Issue("error", "authors_type_invalid", "authors 必须是字符串、字符串数组或 null。")
        )
    for field_name in ("tags", "domain_slugs", "decision_evidence", "warnings"):
        value = record.get(field_name)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            issues.append(
                Issue("error", "field_type_invalid", f"{field_name} 必须是字符串数组或 null。")
            )

    year = _text(record.get("year"))
    year_value = record.get("year")
    if year_value is not None and (
        isinstance(year_value, bool) or not isinstance(year_value, (str, int))
    ):
        issues.append(Issue("error", "year_type_invalid", "year 必须是整数、字符串或 null。"))
    if year and (not year.isdigit() or not 1800 <= int(year) <= 2100):
        issues.append(Issue("error", "year_invalid", f"year 不是合理四位年份：{year}"))

    analysis = record.get("analysis")
    if analysis is not None and not isinstance(analysis, Mapping):
        issues.append(Issue("error", "analysis_invalid", "analysis 必须是对象或 null。"))
    elif isinstance(analysis, Mapping):
        for field_name in (
            "problem",
            "method",
            "results",
            "limitations",
            "relationships",
            "reproduction",
        ):
            value = analysis.get(field_name)
            if value is not None and not (
                isinstance(value, str)
                or (isinstance(value, list) and all(isinstance(item, str) for item in value))
            ):
                issues.append(
                    Issue(
                        "error",
                        "analysis_field_invalid",
                        f"analysis.{field_name} 必须是字符串、字符串数组或 null。",
                    )
                )

    pdf = record.get("pdf")
    if pdf is not None and not isinstance(pdf, Mapping):
        issues.append(Issue("error", "pdf_invalid", "pdf 必须是对象或 null。"))
    elif isinstance(pdf, Mapping):
        for field_name in ("path", "sha256", "version"):
            value = pdf.get(field_name)
            if value is not None and not isinstance(value, str):
                issues.append(
                    Issue(
                        "error",
                        "pdf_field_invalid",
                        f"pdf.{field_name} 必须是字符串或 null。",
                    )
                )
        sha256 = _text(pdf.get("sha256"))
        if sha256 and not SHA256_RE.fullmatch(sha256):
            issues.append(
                Issue("error", "pdf_sha256_invalid", "pdf.sha256 必须是 64 位小写十六进制。")
            )
        for numeric_field in ("size", "page_count"):
            numeric = pdf.get(numeric_field)
            if numeric is not None and (
                not isinstance(numeric, int) or isinstance(numeric, bool) or numeric <= 0
            ):
                issues.append(
                    Issue(
                        "error",
                        f"pdf_{numeric_field}_invalid",
                        f"pdf.{numeric_field} 必须是正整数。",
                    )
                )
        if _text(pdf.get("path")):
            for field_name in ("sha256", "size", "page_count", "version"):
                if not _text(pdf.get(field_name)):
                    issues.append(
                        Issue(
                            "warning",
                            "pdf_metadata_pending",
                            f"已登记 PDF，但 pdf.{field_name} 待核验。",
                        )
                    )

    code = record.get("code")
    if code is not None and not isinstance(code, Mapping):
        issues.append(Issue("error", "code_invalid", "code 必须是对象或 null。"))
    elif isinstance(code, Mapping):
        for field_name in ("status", "url", "local_path", "commit", "license"):
            value = code.get(field_name)
            if value is not None and not isinstance(value, str):
                issues.append(
                    Issue(
                        "error",
                        "code_field_invalid",
                        f"code.{field_name} 必须是字符串或 null。",
                    )
                )
        evidence_value = code.get("evidence")
        if evidence_value is not None and not (
            isinstance(evidence_value, str)
            or (
                isinstance(evidence_value, list)
                and all(isinstance(item, str) for item in evidence_value)
            )
        ):
            issues.append(
                Issue(
                    "error",
                    "code_evidence_invalid",
                    "code.evidence 必须是字符串、字符串数组或 null。",
                )
            )
        status = _text(code.get("status")) or "unknown"
        if status not in CODE_STATUS_LABELS:
            issues.append(Issue("error", "code_status_invalid", f"未知 code.status：{status}"))
        if status == "official_local" and not _text(code.get("local_path")):
            issues.append(
                Issue(
                    "error",
                    "official_code_path_missing",
                    "official_local 必须填写 code.local_path。",
                )
            )
        if status == "official_local" and not _text(code.get("commit")):
            issues.append(
                Issue(
                    "error",
                    "official_code_commit_missing",
                    "official_local 必须填写固定 code.commit。",
                )
            )
        commit = _text(code.get("commit"))
        if (
            status == "official_local"
            and commit
            and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit)
        ):
            issues.append(
                Issue(
                    "error",
                    "official_code_commit_invalid",
                    "official_local 的 code.commit 必须是完整 40/64 位 Git object ID。",
                )
            )
        if status in {"official_local", "official_url_only"} and not _text(code.get("url")):
            issues.append(
                Issue("error", "official_code_url_missing", f"{status} 必须填写官方 code.url。")
            )
        if status != "not_applicable" and not _text_list(code.get("evidence")):
            issues.append(
                Issue(
                    "warning",
                    "code_evidence_pending",
                    f"code.status={status}，但判定证据待核验。",
                )
            )
        if status == "official_local" and not _text(code.get("license")):
            issues.append(
                Issue(
                    "warning",
                    "code_license_pending",
                    "官方本地代码的许可证待核验。",
                )
            )

    source_urls = record.get("source_urls")
    if source_urls is not None and (
        not isinstance(source_urls, Sequence) or isinstance(source_urls, (str, bytes, bytearray))
    ):
        issues.append(Issue("error", "source_urls_invalid", "source_urls 必须是数组或 null。"))
    else:
        for url_item in source_urls or []:
            if isinstance(url_item, Mapping):
                raw_url = url_item.get("url")
                if not _text(raw_url):
                    issues.append(
                        Issue(
                            "error",
                            "source_url_missing",
                            "source_urls 对象必须填写非空 url。",
                        )
                    )
                    continue
                label = url_item.get("label")
                if label is not None and not isinstance(label, str):
                    issues.append(
                        Issue(
                            "error",
                            "source_label_invalid",
                            "source_urls[].label 必须是字符串或 null。",
                        )
                    )
            elif isinstance(url_item, str):
                raw_url = url_item
                if not raw_url.strip():
                    issues.append(
                        Issue("error", "source_url_missing", "source_urls 不得含空字符串。")
                    )
                    continue
            else:
                issues.append(
                    Issue(
                        "error",
                        "source_url_item_invalid",
                        "source_urls 项必须是 URL 字符串或含 url 的对象。",
                    )
                )
                continue
            try:
                normalize_href(raw_url, external_only=True)
            except RecordError as exc:
                issues.append(Issue("error", "source_url_invalid", str(exc)))

    for group, key in ((pdf, "path"), (code, "local_path"), (code, "url")):
        if isinstance(group, Mapping) and _text(group.get(key)):
            try:
                normalize_href(
                    group.get(key),
                    external_only=(key == "url"),
                    local_only=(key != "url"),
                )
            except RecordError as exc:
                issues.append(Issue("error", f"{key}_invalid", str(exc)))
    return issues


def render_record(
    record: Mapping[str, Any], *, template_path: Path, styles_path: Path
) -> tuple[str, list[Issue]]:
    issues = validate_record(record)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise RecordError("；".join(issue.message for issue in errors))

    paper_id = _text(record.get("paper_id"))
    assert paper_id is not None
    title = _text(record.get("title"))
    title_zh = _text(record.get("title_zh"))
    display_title = title_zh or title or PENDING
    original_title = title if title and title != display_title else None
    summary = _text(record.get("summary"))
    abstract = _text(record.get("abstract"))
    description = summary or abstract or f"{paper_id} 论文资料页；部分信息待核验。"
    authors = _text_list(record.get("authors"))
    tags = _text_list(record.get("tags"))
    domain_slugs = _text_list(record.get("domain_slugs"))

    badges: list[str] = []
    for value in (_text(record.get("year")), _text(record.get("venue")), *domain_slugs, *tags):
        if value:
            badges.append(f'<span class="badge">{_escape(value)}</span>')
    if not badges:
        badges.append(f'<span class="badge">{_pending()}</span>')

    hero = (
        '<section class="hero" aria-labelledby="paper-title">'
        f'<div class="eyebrow">Paper record · {_escape(paper_id)}</div>'
        f'<h1 id="paper-title">{_escape(display_title)}</h1>'
        + (
            f'<p class="original-title" lang="en">{_escape(original_title)}</p>'
            if original_title
            else ""
        )
        + f'<div class="lead">{_escape(summary) if summary else _pending()}</div>'
        + f'<div class="badges">{"".join(badges)}</div></section>'
    )

    analysis_raw = record.get("analysis")
    analysis: Mapping[str, Any] = analysis_raw if isinstance(analysis_raw, Mapping) else {}
    sections = [
        _section(
            "overview",
            "Overview",
            "摘要与概述",
            _paragraphs_or_pending(abstract) + _paragraphs_or_pending(summary),
        ),
        _section("problem", "Problem", "研究问题", _paragraphs_or_pending(analysis.get("problem"))),
        _section("method", "Method", "方法与机制", _list_or_pending(analysis.get("method"))),
        _section(
            "results", "Evidence", "实验结果与证据口径", _list_or_pending(analysis.get("results"))
        ),
        _section(
            "limitations",
            "Limitations",
            "限制与失败场景",
            _list_or_pending(analysis.get("limitations")),
        ),
        _section(
            "relationships",
            "Relations",
            "与已有工作的关系",
            _list_or_pending(analysis.get("relationships")),
        ),
    ]

    code_raw = record.get("code")
    code: Mapping[str, Any] = code_raw if isinstance(code_raw, Mapping) else {}
    code_status = _text(code.get("status")) or "unknown"
    code_bits = [f"<p><strong>审计结局：</strong>{_escape(CODE_STATUS_LABELS[code_status])}</p>"]
    code_links: list[str] = []
    code_url = (
        normalize_href(code.get("url"), external_only=True) if _text(code.get("url")) else None
    )
    code_path = (
        normalize_href(code.get("local_path"), local_only=True)
        if _text(code.get("local_path"))
        else None
    )
    if code_path:
        code_links.append(
            _anchor(
                "打开本地代码",
                code_path,
                "code-local",
                data={
                    "code-status": code_status,
                    "expected-commit": code.get("commit"),
                    "expected-license": code.get("license"),
                },
            )
        )
    if code_url:
        code_links.append(_anchor("官方代码仓库", code_url, "code-url"))
    code_bits.append(
        "<p><strong>资源：</strong>"
        + (" · ".join(code_links) if code_links else _pending())
        + "</p>"
    )
    commit_value = _value_or_pending(code.get("commit"), css_class="mono")
    code_bits.append(f"<p><strong>固定提交：</strong>{commit_value}</p>")
    code_bits.append(f"<p><strong>许可证：</strong>{_value_or_pending(code.get('license'))}</p>")
    code_bits.append("<h3>判定证据</h3>" + _list_or_pending(code.get("evidence")))
    sections.append(_section("code", "Code", "代码审计与本地归档", "".join(code_bits)))

    pdf_raw = record.get("pdf")
    pdf: Mapping[str, Any] = pdf_raw if isinstance(pdf_raw, Mapping) else {}
    source_links: list[str] = []
    pdf_path = normalize_href(pdf.get("path"), local_only=True) if _text(pdf.get("path")) else None
    if pdf_path:
        source_links.append(
            _anchor(
                "本地 PDF",
                pdf_path,
                "pdf",
                data={
                    "expected-sha256": pdf.get("sha256"),
                    "expected-size": pdf.get("size"),
                    "expected-pages": pdf.get("page_count"),
                    "version": pdf.get("version"),
                },
            )
        )
    for index, item in enumerate(record.get("source_urls") or [], start=1):
        if isinstance(item, Mapping):
            raw_url = item.get("url")
            label = _text(item.get("label")) or f"来源 {index}"
        else:
            raw_url = item
            label = f"来源 {index}"
        href = normalize_href(raw_url, external_only=True)
        if href:
            source_links.append(_anchor(label, href, "source"))
    source_content = (
        '<ul class="resource-list">'
        + "".join(f"<li>{link}</li>" for link in source_links)
        + "</ul>"
        if source_links
        else f"<p>{_pending()}</p>"
    )
    sections.append(_section("sources", "Sources", "论文、本地文件与外部来源", source_content))

    warning_items = _text_list(record.get("warnings"))
    verification = "<h3>身份与分类证据</h3>" + _list_or_pending(record.get("decision_evidence"))
    verification += "<h3>待处理警告</h3>" + (
        _list_or_pending(warning_items, css_class="warning-list")
        if warning_items
        else "<p>无已登记警告。</p>"
    )
    verification += "<h3>最小复现检查</h3>" + _list_or_pending(analysis.get("reproduction"))
    sections.append(_section("verification", "Verification", "核验、警告与复现", verification))

    authors_html = "、".join(_escape(author) for author in authors) if authors else _pending()
    facts = [
        ("英文正式题名", _value_or_pending(record.get("title")), True),
        ("中文题名", _value_or_pending(record.get("title_zh")), True),
        ("作者", authors_html, True),
        ("年份", _value_or_pending(record.get("year")), True),
        ("发表 venue", _value_or_pending(record.get("venue")), True),
        ("DOI", _value_or_pending(record.get("doi"), css_class="mono"), True),
        ("arXiv", _value_or_pending(record.get("arxiv_id"), css_class="mono"), True),
        ("PDF SHA-256", _value_or_pending(pdf.get("sha256"), css_class="mono"), True),
        ("PDF 大小", _value_or_pending(pdf.get("size")), True),
        ("PDF 页数", _value_or_pending(pdf.get("page_count")), True),
        ("PDF 版本", _value_or_pending(pdf.get("version")), True),
    ]
    facts_html = "".join(
        f"<dt>{_escape(label)}</dt><dd>{value if already_html else _escape(value)}</dd>"
        for label, value, already_html in facts
    )
    aside = (
        '<section class="fact-card"><h2>论文事实</h2><dl class="facts">'
        + facts_html
        + "</dl></section>"
        '<section class="fact-card"><h2>真实性提示</h2>'
        '<p class="muted">页面只呈现输入记录中的证据。缺失值不会被自动补写。</p></section>'
    )

    template = Template(template_path.read_text(encoding="utf-8"))
    rendered = template.substitute(
        paper_id_attr=_escape(paper_id),
        description_attr=_escape(description),
        document_title=_escape(f"{display_title}｜核心领域论文资料库"),
        styles=styles_path.read_text(encoding="utf-8").rstrip(),
        hero=hero,
        sections="\n".join(sections),
        aside=aside,
    )
    return rendered.rstrip() + "\n", issues


class _AuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang: str | None = None
        self.charset: str | None = None
        self.has_viewport = False
        self.has_title = False
        self.in_title = False
        self.in_style = False
        self.title_parts: list[str] = []
        self.style_parts: list[str] = []
        self.style_attributes: list[str] = []
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.duplicate_attributes: list[tuple[str, str]] = []
        self.section_ids: set[str] = set()
        self.links: list[tuple[str, dict[str, str | None]]] = []
        self.subresources: list[tuple[str, str]] = []
        self.prohibited_elements: list[str] = []
        self.dangerous_attributes: list[tuple[str, str]] = []
        self.paper_id: str | None = None
        self.pending_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values: dict[str, str | None] = {}
        for raw_key, value in attrs:
            key = raw_key.lower()
            if key in values:
                self.duplicate_attributes.append((tag, key))
            else:
                # Browsers keep the first duplicate attribute. The validator
                # mirrors that behavior and separately fails the duplicate.
                values[key] = value
        element_id = values.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        if tag == "section" and values.get("data-paper-section"):
            self.section_ids.add(values["data-paper-section"] or "")
        if values.get("data-status") == "pending":
            self.pending_count += 1
        if tag in {"base", "embed", "form", "iframe", "object", "script"}:
            self.prohibited_elements.append(tag)
        for key in values:
            if key.startswith("on"):
                self.dangerous_attributes.append((tag, key))
        if values.get("style"):
            self.style_attributes.append(values["style"] or "")
        if tag == "a" and values.get("ping"):
            self.dangerous_attributes.append((tag, "ping"))
        if tag == "html":
            self.html_lang = values.get("lang")
        elif tag == "meta":
            if values.get("charset"):
                self.charset = values["charset"]
            if (values.get("name") or "").lower() == "viewport":
                self.has_viewport = bool(values.get("content"))
            if (values.get("name") or "").lower() == "paper-id":
                self.paper_id = values.get("content")
            if (values.get("http-equiv") or "").lower() == "refresh":
                self.prohibited_elements.append("meta-refresh")
        elif tag == "title":
            self.in_title = True
            self.has_title = True
        elif tag == "style":
            self.in_style = True
        elif tag == "a" and values.get("href"):
            self.links.append((values["href"] or "", values))

        resource_attributes = {
            "script": ("src",),
            "img": ("src",),
            "source": ("src",),
            "video": ("src", "poster"),
            "audio": ("src",),
            "iframe": ("src",),
            "object": ("data",),
            "embed": ("src",),
            "track": ("src",),
            "input": ("src",),
            "image": ("href", "xlink:href"),
            "use": ("href", "xlink:href"),
        }.get(tag, ())
        for attribute in resource_attributes:
            if values.get(attribute):
                self.subresources.append((f"{tag}[{attribute}]", values[attribute] or ""))
        for attribute in ("background",):
            if values.get(attribute):
                self.subresources.append((f"{tag}[{attribute}]", values[attribute] or ""))
        for attribute in ("srcset",):
            if values.get(attribute):
                for candidate in (values[attribute] or "").split(","):
                    url = candidate.strip().split(maxsplit=1)[0] if candidate.strip() else ""
                    if url:
                        self.subresources.append((f"{tag}[{attribute}]", url))
        if tag == "link":
            rel_tokens = set((values.get("rel") or "").lower().split())
            resource_rels = {
                "apple-touch-icon",
                "icon",
                "manifest",
                "modulepreload",
                "preload",
                "stylesheet",
            }
            if rel_tokens & resource_rels and values.get("href"):
                self.subresources.append(
                    (f"link[{','.join(sorted(rel_tokens))}]", values["href"] or "")
                )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False
        elif tag.lower() == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_style:
            self.style_parts.append(data)


def _is_remote(value: str) -> bool:
    parsed = _parse_url(value.strip())
    return parsed.scheme.lower() in REMOTE_SCHEMES or bool(parsed.netloc)


def _decode_css_escapes(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            try:
                return chr(int(match.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return match.group(2) or ""

    return re.sub(
        r"\\([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|\\(.)",
        replace,
        value,
        flags=re.DOTALL,
    )


def _extract_css_resources(value: str) -> tuple[list[str], bool]:
    decoded = _decode_css_escapes(re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL))
    resources: list[str] = []
    for match in re.finditer(
        r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)'\"\s][^)]*?))\s*\)",
        decoded,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        candidate = next((group for group in match.groups() if group is not None), "").strip()
        if candidate:
            resources.append(candidate)
    has_import = bool(re.search(r"@import\b", decoded, flags=re.IGNORECASE))
    for match in re.finditer(
        r"@import\s+(?:\"([^\"]+)\"|'([^']+)')",
        decoded,
        flags=re.IGNORECASE,
    ):
        candidate = next((group for group in match.groups() if group), "").strip()
        if candidate:
            resources.append(candidate)
    for match in re.finditer(
        r"(?:\"((?:https?:)?//[^\"]+)\"|'((?:https?:)?//[^']+)')",
        decoded,
        flags=re.IGNORECASE,
    ):
        candidate = next((group for group in match.groups() if group), "").strip()
        if candidate:
            resources.append(candidate)
    return resources, has_import


def _inside_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_pdf(path: Path, attrs: Mapping[str, str | None], result: ValidationResult) -> None:
    if not path.is_file():
        result.errors.append(
            Issue("error", "pdf_not_file", f"PDF 链接不是普通文件：{path}", result.path)
        )
        return
    try:
        with path.open("rb") as stream:
            magic = stream.read(5)
        if magic != b"%PDF-":
            result.errors.append(
                Issue("error", "pdf_magic_invalid", f"PDF 魔数无效：{path}", result.path)
            )
            return
        actual_size = path.stat().st_size
        actual_sha256 = _sha256_file(path)
        actual_pages = len(PdfReader(str(path), strict=False).pages)
    except Exception as exc:
        result.errors.append(
            Issue("error", "pdf_verification_failed", f"PDF 核验失败：{path}: {exc}", result.path)
        )
        return

    expected_sha256 = _text(attrs.get("data-expected-sha256"))
    expected_size = _text(attrs.get("data-expected-size"))
    expected_pages = _text(attrs.get("data-expected-pages"))
    for field_name, expected in (
        ("sha256", expected_sha256),
        ("size", expected_size),
        ("pages", expected_pages),
    ):
        if not expected:
            result.warnings.append(
                Issue(
                    "warning",
                    "pdf_expected_metadata_missing",
                    f"PDF 的预期 {field_name} 待核验：{path}",
                    result.path,
                )
            )
    if expected_sha256 and expected_sha256 != actual_sha256:
        result.errors.append(
            Issue(
                "error",
                "pdf_sha256_mismatch",
                f"PDF SHA-256 不匹配：{path}",
                result.path,
            )
        )
    try:
        if expected_size and int(expected_size) != actual_size:
            result.errors.append(
                Issue("error", "pdf_size_mismatch", f"PDF 大小不匹配：{path}", result.path)
            )
        if expected_pages and int(expected_pages) != actual_pages:
            result.errors.append(
                Issue("error", "pdf_pages_mismatch", f"PDF 页数不匹配：{path}", result.path)
            )
    except ValueError:
        result.errors.append(
            Issue(
                "error",
                "pdf_expected_metadata_invalid",
                f"PDF 预期 size/pages 不是整数：{path}",
                result.path,
            )
        )
    result.verified_assets.append(
        {
            "kind": "pdf",
            "path": str(path),
            "sha256": actual_sha256,
            "size": actual_size,
            "page_count": actual_pages,
        }
    )


def _verify_code_directory(
    path: Path, attrs: Mapping[str, str | None], result: ValidationResult
) -> None:
    if not path.is_dir():
        result.errors.append(
            Issue(
                "error",
                "code_not_directory",
                f"本地代码链接不是目录：{path}",
                result.path,
            )
        )
        return
    expected_commit = _text(attrs.get("data-expected-commit"))
    if not expected_commit:
        result.errors.append(
            Issue(
                "error",
                "code_expected_commit_missing",
                f"本地官方代码缺少固定 commit：{path}",
                result.path,
            )
        )
        return
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.errors.append(
            Issue("error", "git_verification_failed", f"Git 核验失败：{path}: {exc}", result.path)
        )
        return
    actual_commit = completed.stdout.strip().lower()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", actual_commit):
        result.errors.append(
            Issue(
                "error",
                "git_head_unavailable",
                f"无法读取本地代码 HEAD：{path}",
                result.path,
            )
        )
        return
    if actual_commit != expected_commit.lower():
        result.errors.append(
            Issue(
                "error",
                "git_commit_mismatch",
                f"本地代码 HEAD 与固定 commit 不一致：{path}",
                result.path,
            )
        )
    try:
        status_result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if status_result.returncode != 0:
            result.errors.append(
                Issue(
                    "error",
                    "git_status_unavailable",
                    f"无法读取本地代码工作树状态：{path}",
                    result.path,
                )
            )
        elif status_result.stdout.strip():
            result.errors.append(
                Issue(
                    "error",
                    "git_worktree_dirty",
                    f"本地官方代码工作树不是固定 commit 的干净快照：{path}",
                    result.path,
                )
            )
    except (OSError, subprocess.SubprocessError) as exc:
        result.errors.append(
            Issue(
                "error",
                "git_status_unavailable",
                f"无法读取本地代码工作树状态：{path}: {exc}",
                result.path,
            )
        )

    expected_license = _text(attrs.get("data-expected-license"))
    license_files = sorted(
        item.name
        for item in path.iterdir()
        if item.is_file()
        and (item.name.lower().startswith("license") or item.name.lower().startswith("copying"))
    )
    undeclared_license_values = {
        "noassertion",
        "none",
        "unlicensed",
        "not-declared",
        "not_declared",
        "unknown",
        "许可未声明",
    }
    if expected_license and expected_license.lower() not in undeclared_license_values:
        if not license_files:
            result.errors.append(
                Issue(
                    "error",
                    "code_license_file_missing",
                    f"声明了许可证但仓库根没有 LICENSE/COPYING：{path}",
                    result.path,
                )
            )
    elif not expected_license:
        result.warnings.append(
            Issue(
                "warning",
                "code_license_pending",
                f"本地官方代码许可证待核验：{path}",
                result.path,
            )
        )
    result.verified_assets.append(
        {
            "kind": "code",
            "path": str(path),
            "commit": actual_commit,
            "license_files": license_files,
        }
    )


def validate_html_file(
    path: Path,
    *,
    check_local_links: bool = True,
    allowed_root: Path | None = None,
) -> ValidationResult:
    result = ValidationResult(path=str(path.resolve()))
    if allowed_root is not None and not _inside_root(path, allowed_root):
        result.errors.append(
            Issue(
                "error",
                "html_outside_allowed_root",
                f"HTML 不在允许的永久资料根内：{allowed_root.resolve()}",
                str(path),
            )
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        result.errors.append(Issue("error", "html_unreadable", str(exc), str(path)))
        return result
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        result.errors.append(Issue("error", "encoding_not_utf8", str(exc), str(path)))
        return result
    if "\ufffd" in text:
        result.errors.append(
            Issue("error", "replacement_character", "HTML 含 Unicode 替换字符。", str(path))
        )
    if not re.match(r"\s*<!doctype\s+html>", text, flags=re.IGNORECASE):
        result.errors.append(Issue("error", "doctype_missing", "缺少 HTML5 DOCTYPE。", str(path)))

    parser = _AuditParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # HTMLParser can surface malformed entity edge cases.
        result.errors.append(Issue("error", "html_parse_error", str(exc), str(path)))
        return result
    result.paper_id = parser.paper_id

    if (parser.html_lang or "").lower() != "zh-cn":
        result.errors.append(Issue("error", "lang_invalid", "html lang 必须是 zh-CN。", str(path)))
    if (parser.charset or "").lower() != "utf-8":
        result.errors.append(
            Issue("error", "charset_missing", "缺少 meta charset=utf-8。", str(path))
        )
    if not parser.has_viewport:
        result.errors.append(Issue("error", "viewport_missing", "缺少响应式 viewport。", str(path)))
    if not parser.has_title or not "".join(parser.title_parts).strip():
        result.errors.append(Issue("error", "title_missing", "缺少非空 title。", str(path)))
    missing_sections = sorted(set(REQUIRED_SECTION_IDS) - parser.section_ids)
    if missing_sections:
        result.errors.append(
            Issue(
                "error",
                "required_sections_missing",
                f"缺少必需章节：{', '.join(missing_sections)}",
                str(path),
            )
        )
    if not parser.paper_id or not PAPER_ID_RE.fullmatch(parser.paper_id):
        result.errors.append(
            Issue("error", "paper_id_meta_invalid", "meta paper-id 缺失或不合法。", str(path))
        )

    if parser.duplicate_ids:
        result.errors.append(
            Issue(
                "error",
                "duplicate_ids",
                "存在重复 id：" + ", ".join(sorted(parser.duplicate_ids)),
                str(path),
            )
        )
    if parser.duplicate_attributes:
        attributes = ", ".join(
            f"{tag}[{attribute}]" for tag, attribute in parser.duplicate_attributes
        )
        result.errors.append(
            Issue(
                "error",
                "duplicate_attributes",
                f"HTML 含重复属性，浏览器解释不唯一：{attributes}",
                str(path),
            )
        )
    if parser.prohibited_elements:
        result.errors.append(
            Issue(
                "error",
                "prohibited_elements",
                "静态论文页禁止以下元素：" + ", ".join(sorted(set(parser.prohibited_elements))),
                str(path),
            )
        )
    if parser.dangerous_attributes:
        attributes = ", ".join(
            f"{tag}[{attribute}]" for tag, attribute in parser.dangerous_attributes
        )
        result.errors.append(
            Issue(
                "error",
                "event_handler_attributes",
                f"静态论文页禁止内联事件处理器：{attributes}",
                str(path),
            )
        )

    css_import_found = False
    for css_value in ["\n".join(parser.style_parts), *parser.style_attributes]:
        css_resources, has_import = _extract_css_resources(css_value)
        css_import_found = css_import_found or has_import
        parser.subresources.extend(("css[url]", item) for item in css_resources)
    if css_import_found:
        result.errors.append(
            Issue(
                "error",
                "css_import_forbidden",
                "自包含论文页禁止 CSS @import。",
                str(path),
            )
        )

    for tag, value in parser.subresources:
        try:
            normalize_href(value)
            parsed_resource = _parse_url(value)
        except RecordError as exc:
            result.errors.append(
                Issue("error", "subresource_url_invalid", f"{tag}: {exc}", str(path))
            )
            continue
        scheme = parsed_resource.scheme.lower()
        if _is_remote(value):
            result.errors.append(
                Issue(
                    "error",
                    "remote_subresource",
                    f"离线页面禁止远程 {tag} 子资源：{value}",
                    str(path),
                )
            )
        elif scheme in FORBIDDEN_SCHEMES or (scheme and scheme not in REMOTE_SCHEMES):
            result.errors.append(
                Issue(
                    "error",
                    "unsafe_subresource_scheme",
                    f"不安全的 {tag} 子资源：{value}",
                    str(path),
                )
            )
        elif value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            result.errors.append(
                Issue(
                    "error",
                    "absolute_local_subresource",
                    f"本地 {tag} 子资源必须使用相对路径：{value}",
                    str(path),
                )
            )
        elif value:
            local_part = _fully_unquote(parsed_resource.path)
            candidate = (path.parent / local_part).resolve()
            if (
                local_part
                and allowed_root is not None
                and not _inside_root(candidate, allowed_root)
            ):
                result.errors.append(
                    Issue(
                        "error",
                        "local_subresource_outside_allowed_root",
                        f"本地 {tag} 子资源越出允许根：{value}",
                        str(path),
                    )
                )
            if check_local_links and local_part and not candidate.exists():
                result.errors.append(
                    Issue(
                        "error",
                        "local_subresource_missing",
                        f"本地 {tag} 子资源不存在：{value}",
                        str(path),
                    )
                )

    for href, attrs in parser.links:
        result.links_checked += 1
        try:
            normalize_href(href)
            parsed = _parse_url(href)
        except RecordError as exc:
            result.errors.append(Issue("error", "unsafe_link", str(exc), str(path)))
            continue
        scheme = parsed.scheme.lower()
        if scheme in FORBIDDEN_SCHEMES or (scheme and scheme not in REMOTE_SCHEMES):
            result.errors.append(
                Issue("error", "unsafe_link_scheme", f"不安全链接：{href}", str(path))
            )
            continue
        if scheme in REMOTE_SCHEMES:
            if not parsed.netloc:
                result.errors.append(
                    Issue("error", "external_link_invalid", f"外部链接缺少主机：{href}", str(path))
                )
            continue
        if href.startswith("/") or re.match(r"^[A-Za-z]:", href):
            result.errors.append(
                Issue(
                    "error", "absolute_local_link", f"本地链接必须使用相对路径：{href}", str(path)
                )
            )
            continue
        if href.startswith("#"):
            fragment = _fully_unquote(parsed.fragment)
            if fragment and fragment not in parser.ids:
                result.errors.append(
                    Issue("error", "anchor_missing", f"锚点不存在：{href}", str(path))
                )
            continue
        local_part = _fully_unquote(parsed.path)
        if not local_part:
            continue
        candidate = (path.parent / local_part).resolve()
        if allowed_root is not None and not _inside_root(candidate, allowed_root):
            result.errors.append(
                Issue(
                    "error",
                    "local_link_outside_allowed_root",
                    f"本地链接越出允许根：{href}",
                    str(path),
                )
            )
        if check_local_links and not candidate.exists():
            result.errors.append(
                Issue("error", "local_link_missing", f"本地链接不存在：{href}", str(path))
            )
            continue
        link_kind = _text(attrs.get("data-link-kind"))
        if candidate.exists() and link_kind == "pdf":
            _verify_pdf(candidate, attrs, result)
        elif candidate.exists() and link_kind == "code-local":
            _verify_code_directory(candidate, attrs, result)

    if parser.pending_count:
        result.warnings.append(
            Issue(
                "warning",
                "metadata_pending",
                f"页面仍有 {parser.pending_count} 个待核验标记。",
                str(path),
            )
        )
    return result


def summarize_results(results: Iterable[ValidationResult]) -> dict[str, Any]:
    items = list(results)
    errors = sum(len(item.errors) for item in items)
    warnings = sum(len(item.warnings) for item in items)
    return {
        "schema_version": "paper-intake-html.validation.v1",
        "status": "passed" if errors == 0 else "failed",
        "files_checked": len(items),
        "files_passed": sum(1 for item in items if item.passed),
        "errors": errors,
        "warnings": warnings,
        "links_checked": sum(item.links_checked for item in items),
        "results": [item.to_dict() for item in items],
    }
