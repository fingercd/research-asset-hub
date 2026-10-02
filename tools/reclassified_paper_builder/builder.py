from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pypdf import PdfReader

HUB_ROOT = Path(__file__).resolve().parents[2]
HTML_TOOL_DIR = HUB_ROOT / "tools" / "paper_intake_html"
SCHEMA_VERSION = "reclassified-paper-builder.report.v1"
MANIFEST_SCHEMA = "reclassified-paper-builder.outputs-manifest.v1"
METADATA_SCHEMA = "reclassified-paper.metadata.v1"
CATALOG_SCHEMA = "reclassified-paper.domain-catalog.v1"
TARGET_DIRECTORY = Path("个人新增资料") / "2026-08-31-顶会压缩论文"
MANIFEST_NAME = ".reclassified-paper-builder.manifest.json"
TRANSACTION_NAME = ".reclassified-paper-builder.transaction.json"
DOMAIN_LABELS = {
    "agent": "智能体",
    "fine-tuning": "微调",
    "generation": "生成",
    "infra": "AI 系统与基础设施",
    "world-model": "世界模型",
}
DEFAULT_EXPECTED_COUNTS = {
    "agent": 15,
    "fine-tuning": 2,
    "generation": 15,
    "infra": 183,
    "world-model": 2,
}
NO_CODE_STATUSES = {
    "announced_pending",
    "none_found",
    "not_applicable",
    "third_party_only",
}
HTML_CODE_STATUSES = {
    "official_local",
    "official_url_only",
    *NO_CODE_STATUSES,
    "unknown",
}
PAPER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIMITATION_RE = re.compile(
    r"\b(?:limitation|limitations|future work|future direction|we leave|failure|"
    r"drawback|constraint|shortcoming)\b",
    re.IGNORECASE,
)
SENSITIVE_QUERY_KEYS = {
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


class BuildError(RuntimeError):
    """Raised when the builder cannot safely stage or apply its output."""


@dataclass(frozen=True)
class BuildResult:
    status: str
    build_id: str
    staging_root: Path
    target_root: Path
    counts: Mapping[str, int]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    applied: bool
    files_created: int = 0
    files_updated: int = 0
    files_unchanged: int = 0


@dataclass(frozen=True)
class PdfFacts:
    path: Path
    sha256: str
    size: int
    pages: int


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, _json_bytes(value))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _ensure_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    if not _inside(resolved, root):
        raise BuildError(f"{label}越出核心领域根：{path}")
    return resolved


def _relative_link(target: Path, page: Path) -> str:
    return os.path.relpath(target, page.parent).replace("\\", "/")


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float)):
        result = str(value).strip()
        return result or None
    return None


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [text for item in value if (text := _text(item))]
    return []


def _load_records(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"无法读取 final paper-record：{path}: {exc}") from exc
    records: Any = None
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("records", "papers", "items"):
            if key in payload:
                records = payload[key]
                break
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise BuildError("final paper-record 必须是对象数组或 records/papers/items 包装对象。")
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in records:
        if _text(raw.get("disposition")) != "reclassify-retain":
            continue
        domains = [item for item in _text_list(raw.get("domain_slugs")) if item in DOMAIN_LABELS]
        if not domains:
            continue
        if len(domains) != 1:
            raise BuildError(f"记录命中多个转投领域：{raw.get('paper_id')}: {domains}")
        paper_id = _text(raw.get("paper_id"))
        if not paper_id or not PAPER_ID_RE.fullmatch(paper_id):
            raise BuildError(f"paper_id 不合法：{paper_id!r}")
        if paper_id in seen:
            raise BuildError(f"转投记录 paper_id 重复：{paper_id}")
        seen.add(paper_id)
        record = dict(raw)
        record["_reclassified_domain"] = domains[0]
        selected.append(record)
    return sorted(selected, key=lambda item: (item["_reclassified_domain"], item["paper_id"]))


def _load_evidence(evidence_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not evidence_dir.is_dir():
        raise BuildError(f"evidence-dir 不是目录：{evidence_dir}")
    result: dict[str, dict[str, Any]] = {}
    ignored: list[str] = []
    for path in sorted(evidence_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"证据 JSON 无法读取：{path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            "paper-analysis-evidence.v1"
        ):
            ignored.append(str(path))
            continue
        paper = payload.get("paper")
        paper_id = _text(paper.get("paper_id")) if isinstance(paper, Mapping) else None
        if not paper_id:
            raise BuildError(f"证据缺少 paper.paper_id：{path}")
        if paper_id in result:
            raise BuildError(f"证据 paper_id 重复：{paper_id}")
        payload["_source_path"] = str(path.resolve())
        result[paper_id] = payload
    return result, ignored


def _load_code_audits(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"代码审计 JSON 无法读取：{path}: {exc}") from exc
        papers = payload.get("papers") if isinstance(payload, Mapping) else None
        if not isinstance(papers, list):
            raise BuildError(f"代码审计缺少 papers 数组：{path}")
        for item in papers:
            if not isinstance(item, dict):
                raise BuildError(f"代码审计 papers 含非对象项：{path}")
            paper_id = _text(item.get("paper_id"))
            if not paper_id:
                raise BuildError(f"代码审计记录缺少 paper_id：{path}")
            if paper_id in result and result[paper_id] != item:
                raise BuildError(f"多个代码审计对 {paper_id} 给出冲突结论。")
            result[paper_id] = dict(item)
    return result


def _safe_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise BuildError("外部 URL 语法无效。") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BuildError(f"外部 URL 仅允许 http/https：{text}")
    if parsed.username or parsed.password:
        raise BuildError("外部 URL 不得包含用户名或密码。")
    if any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query)):
        raise BuildError("外部 URL 不得包含凭据或签名查询参数。")
    return text


def _pdf_facts(path: Path) -> PdfFacts:
    if not path.is_file():
        raise BuildError(f"PDF 不存在：{path}")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise BuildError(f"PDF 魔数错误：{path}")
    try:
        pages = len(PdfReader(str(path)).pages)
    except Exception as exc:
        raise BuildError(f"PDF 无法解析：{path}: {exc}") from exc
    if pages < 1:
        raise BuildError(f"PDF 页数为 0：{path}")
    return PdfFacts(path=path, sha256=_sha256_file(path), size=path.stat().st_size, pages=pages)


def _evidence_item_line(item: Mapping[str, Any]) -> str:
    page = item.get("page")
    section = _text(item.get("section")) or "未标注章节"
    excerpt = _text(item.get("excerpt")) or ""
    return f"原文证据（第 {page} 页，{section}）：{excerpt}"


def _validated_evidence(
    evidence: Mapping[str, Any], facts: PdfFacts, paper_id: str
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    if evidence.get("status") != "ok":
        blockers.append(f"analysis_evidence_error:{paper_id}")
    sources = evidence.get("sources")
    pdf_source = sources.get("pdf") if isinstance(sources, Mapping) else None
    if not isinstance(pdf_source, Mapping):
        blockers.append(f"analysis_evidence_pdf_source_missing:{paper_id}")
    else:
        if _text(pdf_source.get("sha256")) != facts.sha256:
            blockers.append(f"analysis_evidence_pdf_sha256_mismatch:{paper_id}")
        if pdf_source.get("page_count") != facts.pages:
            blockers.append(f"analysis_evidence_page_count_mismatch:{paper_id}")
    evidence_groups = evidence.get("evidence")
    if not isinstance(evidence_groups, Mapping):
        blockers.append(f"analysis_evidence_groups_missing:{paper_id}")
        evidence_groups = {}
    clean_groups: dict[str, list[dict[str, Any]]] = {}
    for category in (
        "abstract",
        "introduction_contributions",
        "method",
        "experiments_results",
        "limitations_conclusion",
        "code_urls",
    ):
        values = evidence_groups.get(category)
        if not isinstance(values, list):
            blockers.append(f"analysis_evidence_category_invalid:{paper_id}:{category}")
            values = []
        clean: list[dict[str, Any]] = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                blockers.append(f"analysis_evidence_item_invalid:{paper_id}:{category}:{index}")
                continue
            page = item.get("page")
            excerpt = _text(item.get("excerpt"))
            excerpt_sha = _text(item.get("excerpt_sha256"))
            if not isinstance(page, int) or not 1 <= page <= facts.pages:
                blockers.append(f"analysis_evidence_page_invalid:{paper_id}:{category}:{index}")
            if not excerpt or excerpt_sha != _sha256_bytes(excerpt.encode("utf-8")):
                blockers.append(
                    f"analysis_evidence_excerpt_hash_mismatch:{paper_id}:{category}:{index}"
                )
            clean.append(dict(item))
        clean_groups[category] = clean
    output = {key: value for key, value in evidence.items() if key != "_source_path"}
    output["evidence"] = clean_groups
    return output, blockers


def _analysis_from_evidence(
    record: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    raw_groups = evidence.get("evidence")
    groups: Mapping[str, Any] = raw_groups if isinstance(raw_groups, Mapping) else {}

    def lines(category: str) -> list[str]:
        values = groups.get(category)
        return [_evidence_item_line(item) for item in values or [] if isinstance(item, Mapping)]

    problem = lines("introduction_contributions") or lines("abstract")
    method = lines("method")
    results = lines("experiments_results")
    limitations = [
        _evidence_item_line(item)
        for item in (groups.get("limitations_conclusion") or [])
        if isinstance(item, Mapping)
        and LIMITATION_RE.search(
            f"{_text(item.get('section')) or ''} {_text(item.get('excerpt')) or ''}"
        )
    ]
    analysis = {
        "problem": problem,
        "method": method,
        "results": results,
        "limitations": limitations,
        "relationships": [],
        "reproduction": [],
    }
    abstract = _text(record.get("abstract"))
    summary = _text(record.get("summary"))
    sections: list[str] = [
        f"# {_text(record.get('title_zh')) or _text(record.get('title')) or record['paper_id']}",
        "",
        "本页只整理已核验的论文摘要与逐页原文证据；不依据文件名或模型常识补写事实。",
        "",
        "## 摘要与中文概述",
        "",
        summary or "中文概述待核验。以下保留已核验的原始摘要作为依据。",
        "",
        abstract or "原始摘要待核验。",
    ]
    labels: tuple[tuple[str, list[str]], ...] = (
        ("研究问题", problem),
        ("方法", method),
        ("实验结果", results),
        ("局限与未来工作", limitations),
        ("与已有工作的关系", []),
    )
    for label, values in labels:
        sections.extend(["", f"## {label}", ""])
        if values:
            sections.extend(f"- {value}" for value in values)
        else:
            sections.append("待核验。")
    sections.extend(
        [
            "",
            "## 证据边界",
            "",
            "问题、方法、结果和局限仅在对应逐页证据存在时呈现；缺证据项目保持“待核验”。",
            "",
        ]
    )
    return analysis, "\n".join(sections)


def _git_head(path: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip().lower() if completed.returncode == 0 else None


def _code_for_record(
    record: Mapping[str, Any], audit: Mapping[str, Any] | None, core_root: Path, html_path: Path
) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    paper_id = str(record["paper_id"])
    blockers: list[str] = []
    warnings: list[str] = []
    raw_source_code = record.get("code")
    source_code: Mapping[str, Any] = raw_source_code if isinstance(raw_source_code, Mapping) else {}
    evidence = _text_list(source_code.get("evidence"))
    if audit is None:
        blockers.append(f"code_audit_missing:{paper_id}")
        missing_metadata = {
            "status": "unknown",
            "url": None,
            "local_path": None,
            "commit": None,
            "license": None,
            "evidence": evidence,
        }
        return missing_metadata, dict(missing_metadata), blockers, warnings

    audit_status = _text(audit.get("status")) or "unknown"
    html_mapping = audit.get("html_api_mapping")
    html_status = (
        _text(html_mapping.get("html_code_status")) if isinstance(html_mapping, Mapping) else None
    )
    selected = audit.get("selected_local_archive")
    official_urls = [_safe_url(item) for item in _text_list(audit.get("official_urls"))]
    official_urls = [item for item in official_urls if item]
    if audit_status == "official_local":
        if not isinstance(selected, Mapping):
            blockers.append(f"official_code_archive_missing:{paper_id}")
            selected = {}
        raw_path = _text(selected.get("path"))
        repo = Path(raw_path).resolve(strict=False) if raw_path else core_root / ".missing-code"
        expected_commit = (_text(selected.get("expected_commit")) or "").lower()
        url = _safe_url(selected.get("url")) or (official_urls[0] if official_urls else None)
        if not raw_path or not repo.is_dir() or not _inside(repo, core_root):
            blockers.append(f"official_code_local_path_invalid:{paper_id}")
        if not COMMIT_RE.fullmatch(expected_commit):
            blockers.append(f"official_code_commit_invalid:{paper_id}")
        elif _git_head(repo) != expected_commit:
            blockers.append(f"official_code_commit_mismatch:{paper_id}")
        if not url:
            blockers.append(f"official_code_url_missing:{paper_id}")
        evidence.extend(
            [
                f"final code audit status: {audit_status}",
                *(f"official URL: {item}" for item in official_urls),
            ]
        )
        license_value = _text(selected.get("effective_license"))
        metadata = {
            "status": "official_local",
            "url": url,
            "local_path": (
                repo.relative_to(core_root).as_posix() if _inside(repo, core_root) else None
            ),
            "commit": expected_commit or None,
            "license": license_value,
            "evidence": list(dict.fromkeys(evidence)),
        }
        html_code = dict(metadata)
        html_code["local_path"] = _relative_link(repo, html_path) if repo.is_dir() else None
        if (license_value or "").lower() in {"not-declared", "unknown", "none"}:
            html_code["license"] = None
    elif audit_status == "official_url_only":
        url = official_urls[0] if official_urls else _safe_url(source_code.get("url"))
        if not url:
            blockers.append(f"official_code_url_missing:{paper_id}")
        blockers.append(f"official_code_not_archived:{paper_id}")
        metadata = {
            "status": "official_url_only",
            "url": url,
            "local_path": None,
            "commit": _text(source_code.get("commit")),
            "license": _text(source_code.get("license")),
            "evidence": list(dict.fromkeys([*evidence, "final code audit: official_url_only"])),
        }
        html_code = dict(metadata)
    elif audit_status == "not_provided":
        detailed_status = _text(source_code.get("research_status")) or _text(
            source_code.get("status")
        )
        if detailed_status not in NO_CODE_STATUSES:
            detailed_status = html_status if html_status in NO_CODE_STATUSES else "unknown"
        review = audit.get("no_code_review")
        reviewed_title = _text(review.get("exact_title")) if isinstance(review, Mapping) else None
        if reviewed_title != _text(record.get("title")):
            blockers.append(f"no_code_exact_title_mismatch:{paper_id}")
        if detailed_status not in NO_CODE_STATUSES:
            blockers.append(f"no_code_status_unresolved:{paper_id}")
            detailed_status = "unknown"
        metadata = {
            "status": detailed_status,
            "url": None,
            "local_path": None,
            "commit": None,
            "license": None,
            "evidence": list(
                dict.fromkeys([*evidence, f"final code audit: not_provided ({detailed_status})"])
            ),
            "exact_title_reviewed": reviewed_title,
        }
        html_code = dict(metadata)
    else:
        blockers.append(f"code_audit_unresolved:{paper_id}:{audit_status}")
        status = html_status if html_status in HTML_CODE_STATUSES else "unknown"
        metadata = {
            "status": status,
            "url": official_urls[0] if official_urls else None,
            "local_path": None,
            "commit": None,
            "license": None,
            "evidence": evidence,
        }
        html_code = dict(metadata)
    return metadata, html_code, blockers, warnings


_HTML_MODULE: Any | None = None


def _html_module() -> Any:
    global _HTML_MODULE
    if _HTML_MODULE is not None:
        return _HTML_MODULE
    path = HTML_TOOL_DIR / "paper_html.py"
    spec = importlib.util.spec_from_file_location("reclassified_builder_paper_html", path)
    if spec is None or spec.loader is None:
        raise BuildError("无法载入 paper_intake_html。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _HTML_MODULE = module
    return module


def _render_paper_html(record: Mapping[str, Any], stage_path: Path, core_root: Path) -> list[str]:
    module = _html_module()
    try:
        rendered, issues = module.render_record(
            record,
            template_path=HTML_TOOL_DIR / "template.html",
            styles_path=HTML_TOOL_DIR / "styles.css",
        )
    except Exception as exc:
        raise BuildError(f"paper_intake_html 生成失败：{record.get('paper_id')}: {exc}") from exc
    _write_bytes(stage_path, rendered.encode("utf-8"))
    result = module.validate_html_file(stage_path, check_local_links=False, allowed_root=core_root)
    if result.errors:
        details = "; ".join(f"{item.code}:{item.message}" for item in result.errors)
        raise BuildError(f"paper_intake_html staging 验收失败：{record.get('paper_id')}: {details}")
    return [
        f"{record.get('paper_id')}:{item.code}:{item.message}"
        for item in issues
        if item.level == "warning"
    ]


def _domain_index(domain: str, records: Sequence[Mapping[str, Any]], snapshot_date: str) -> str:
    rows = []
    for record in records:
        paper_id = html.escape(str(record["paper_id"]), quote=True)
        title = html.escape(str(record.get("title") or paper_id))
        year = html.escape(str(record.get("year") or "待核验"))
        venue = html.escape(str(record.get("venue") or "待核验"))
        code_status = html.escape(str((record.get("code") or {}).get("status") or "unknown"))
        rows.append(
            "<tr>"
            f'<td><a href="{paper_id}/paper.html">{title}</a></td>'
            f"<td>{year}</td><td>{venue}</td><td>{code_status}</td>"
            f'<td><a href="{paper_id}/paper.pdf">PDF</a></td>'
            "</tr>"
        )
    label = html.escape(DOMAIN_LABELS[domain])
    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{label}｜2026-08-31 转投论文</title>"
        "<style>body{font:16px/1.6 system-ui,sans-serif;max-width:1200px;margin:auto;"
        "padding:2rem;color:#172033}table{border-collapse:collapse;width:100%}th,td{border-bottom:"
        "1px solid #dfe5ef;padding:.65rem;text-align:left}a{color:#1559b3}"
        "@media(max-width:700px){table{display:block;overflow-x:auto}body{padding:1rem}}</style>"
        f"</head><body><main><h1>{label}</h1><p>快照日期：{snapshot_date}；论文数："
        f"{len(records)}。每篇均有本地 PDF、元数据、证据包和论文 HTML；无 guide 表示。</p>"
        "<table><thead><tr><th>论文</th><th>年份</th><th>Venue</th><th>代码审计</th>"
        f"<th>PDF</th></tr></thead><tbody>{''.join(rows)}</tbody></table></main></body></html>\n"
    )


def _domain_readme(domain: str, count: int, snapshot_date: str) -> str:
    return "\n".join(
        [
            f"# {DOMAIN_LABELS[domain]} · 2026-08-31 转投论文",
            "",
            f"- 快照日期：{snapshot_date}",
            f"- 正式论文：{count}",
            "- 单篇目录：`<paper_id>/paper.pdf`、`paper.html`、`metadata.json`、",
            "  `analysis-evidence.json`、`analysis.zh.md`",
            "- 领域目录：`catalog.json`、`index.html`、`README.md`",
            "- 新数据不创建或登记 guide 表示。",
            "",
            "所有解读内容仅来自 final paper-record 的摘要与逐页 evidence；缺证据项保持待核验。",
            "",
        ]
    )


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _current_owned(target_root: Path) -> tuple[set[str], dict[str, str]]:
    manifest_path = target_root / MANIFEST_NAME
    if not manifest_path.exists():
        return set(), {}
    if not manifest_path.is_file():
        raise BuildError(f"既有构建器 manifest 不是普通文件：{manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"既有构建器 manifest 无法读取：{manifest_path}: {exc}") from exc
    if payload.get("schema_version") != MANIFEST_SCHEMA:
        raise BuildError(f"既有 manifest 不属于本构建器：{manifest_path}")
    stored_hash = payload.pop("manifest_payload_sha256", None)
    if stored_hash != _sha256_bytes(_json_bytes(payload)):
        raise BuildError(f"既有构建器 manifest 已被修改或损坏：{manifest_path}")
    owned = {str(item) for item in payload.get("owned_paths") or []}
    hashes = {
        str(item["path"]): str(item["sha256"])
        for item in payload.get("artifacts") or []
        if isinstance(item, Mapping) and item.get("path") and item.get("sha256")
    }
    return owned, hashes


def _validate_applied(target_root: Path, core_root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = target_root / MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"manifest_unreadable:{exc}"]
    stored_hash = payload.pop("manifest_payload_sha256", None)
    if stored_hash != _sha256_bytes(_json_bytes(payload)):
        errors.append("manifest_payload_hash_mismatch")
    for artifact in payload.get("artifacts") or []:
        path = target_root / Path(str(artifact.get("path")))
        if not _inside(path, target_root) or not path.is_file():
            errors.append(f"artifact_missing:{artifact.get('path')}")
        elif _sha256_file(path) != artifact.get("sha256"):
            errors.append(f"artifact_hash_mismatch:{artifact.get('path')}")
    module = _html_module()
    for domain in DOMAIN_LABELS:
        for paper_dir in sorted((target_root / domain).iterdir()):
            if not paper_dir.is_dir():
                continue
            metadata_path = paper_dir / "metadata.json"
            html_path = paper_dir / "paper.html"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"metadata_unreadable:{domain}/{paper_dir.name}:{exc}")
                continue
            try:
                facts = _pdf_facts(paper_dir / "paper.pdf")
            except BuildError as exc:
                errors.append(f"pdf_invalid:{domain}/{paper_dir.name}:{exc}")
                continue
            pdf = metadata.get("pdf") or {}
            if (
                pdf.get("sha256") != facts.sha256
                or pdf.get("size") != facts.size
                or pdf.get("page_count") != facts.pages
            ):
                errors.append(f"pdf_metadata_mismatch:{domain}/{paper_dir.name}")
            result = module.validate_html_file(
                html_path, check_local_links=True, allowed_root=core_root
            )
            errors.extend(
                f"html_invalid:{domain}/{paper_dir.name}:{item.code}:{item.message}"
                for item in result.errors
            )
    return errors


def _safe_transaction_relative(value: Any, label: str) -> Path:
    text = _text(value)
    if not text:
        raise BuildError(f"事务日志缺少{label}。")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BuildError(f"事务日志中的{label}不是安全相对路径：{text}")
    return path


def _write_durable_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool = False) -> None:
    data = _json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise BuildError(f"已有未完成事务日志，拒绝并发 apply：{path}") from exc
        return
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".staged", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _pid_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _recover_incomplete_transaction(target_root: Path, *, allow_active_owner: bool = False) -> bool:
    journal_path = target_root / TRANSACTION_NAME
    if not journal_path.exists():
        return False
    if not journal_path.is_file():
        raise BuildError(f"事务日志不是普通文件：{journal_path}")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"事务日志无法读取，拒绝继续：{journal_path}: {exc}") from exc
    if not isinstance(journal, dict) or journal.get("schema_version") != (
        "reclassified-paper-builder.transaction.v1"
    ):
        raise BuildError(f"事务日志 schema 不属于本构建器：{journal_path}")
    owner_pid = journal.get("owner_pid")
    if (
        journal.get("status") == "prepared"
        and isinstance(owner_pid, int)
        and _pid_is_running(owner_pid)
        and not allow_active_owner
    ):
        raise BuildError(f"检测到仍在运行的并发 apply（PID {owner_pid}），拒绝恢复其事务。")
    transaction_rel = _safe_transaction_relative(journal.get("transaction_dir"), "transaction_dir")
    transaction_dir = target_root / transaction_rel
    if not _inside(transaction_dir, target_root):
        raise BuildError(f"事务备份目录越界：{transaction_dir}")
    operations = journal.get("operations")
    if not isinstance(operations, list):
        raise BuildError(f"事务日志 operations 无效：{journal_path}")
    if journal.get("status") == "committed":
        if transaction_dir.exists():
            shutil.rmtree(transaction_dir)
        journal_path.unlink()
        return True
    if journal.get("status") != "prepared":
        raise BuildError(f"事务日志状态无效：{journal.get('status')!r}")
    for operation in reversed(operations):
        if not isinstance(operation, Mapping) or not isinstance(operation.get("existed"), bool):
            raise BuildError(f"事务日志操作无效：{operation!r}")
        rel = _safe_transaction_relative(operation.get("path"), "operation.path")
        target = target_root / rel
        backup = transaction_dir / "backups" / rel
        if not _inside(target, target_root) or not _inside(backup, transaction_dir):
            raise BuildError(f"事务恢复路径越界：{rel.as_posix()}")
        if operation["existed"]:
            if backup.is_file():
                if target.exists() and not target.is_file():
                    raise BuildError(f"事务恢复目标不是普通文件：{target}")
                target.unlink(missing_ok=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            elif not target.is_file():
                raise BuildError(f"事务恢复缺少目标与备份：{target}")
        elif target.exists():
            if not target.is_file():
                raise BuildError(f"事务恢复拒绝删除非普通文件：{target}")
            target.unlink()
    if transaction_dir.exists():
        shutil.rmtree(transaction_dir)
    journal_path.unlink()
    return True


def _apply_payload(
    payload_root: Path, target_root: Path, core_root: Path, planned: Sequence[str]
) -> tuple[int, int, int]:
    _recover_incomplete_transaction(target_root)
    owned, old_hashes = _current_owned(target_root)
    prepared: list[tuple[Path, Path, bool]] = []
    created = updated = unchanged = 0
    for rel in planned:
        source = payload_root / Path(rel)
        target = target_root / Path(rel)
        if not _inside(source, payload_root) or not _inside(target, target_root):
            raise BuildError(f"计划路径越界：{rel}")
        if target.exists() and not target.is_file():
            raise BuildError(f"目标存在且不是普通文件：{target}")
        source_hash = _sha256_file(source)
        if target.is_file():
            current_hash = _sha256_file(target)
            if current_hash == source_hash:
                unchanged += 1
                prepared.append((source, target, False))
                continue
            if rel == MANIFEST_NAME:
                if rel not in owned:
                    raise BuildError(f"既有 manifest 未声明自身所有权：{target}")
            elif rel not in owned or old_hashes.get(rel) != current_hash:
                raise BuildError(f"目标不是构建器拥有的未改动文件，拒绝覆盖：{target}")
            updated += 1
        else:
            created += 1
        prepared.append((source, target, True))

    transaction_dir = Path(
        tempfile.mkdtemp(prefix=".reclassified-paper-builder.transaction-", dir=target_root)
    )
    journal_path = target_root / TRANSACTION_NAME
    operations = [
        {
            "path": target.relative_to(target_root).as_posix(),
            "existed": target.is_file(),
        }
        for _source, target, needs_write in prepared
        if needs_write
    ]
    journal: dict[str, Any] = {
        "schema_version": "reclassified-paper-builder.transaction.v1",
        "status": "prepared",
        "owner_pid": os.getpid(),
        "transaction_dir": transaction_dir.relative_to(target_root).as_posix(),
        "operations": operations,
    }
    try:
        _write_durable_json(journal_path, journal, exclusive=True)
        for source, target, needs_write in prepared:
            if not needs_write:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = transaction_dir / "backups" / target.relative_to(target_root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
            os.replace(source, target)
        errors = _validate_applied(target_root, core_root)
        if errors:
            raise BuildError("apply 后验收失败：\n" + "\n".join(errors[:100]))
        journal["status"] = "committed"
        _write_durable_json(journal_path, journal)
    except Exception:
        if journal_path.exists():
            _recover_incomplete_transaction(target_root, allow_active_owner=True)
        elif transaction_dir.exists():
            shutil.rmtree(transaction_dir)
        raise
    else:
        _recover_incomplete_transaction(target_root, allow_active_owner=True)
    return created, updated, unchanged


def build_reclassified_papers(
    *,
    paper_records: Path,
    evidence_dir: Path,
    code_audits: Sequence[Path],
    core_root: Path,
    staging_root: Path,
    snapshot_date: str,
    target_root: Path | None = None,
    apply: bool = False,
) -> BuildResult:
    if not DATE_RE.fullmatch(snapshot_date):
        raise BuildError("snapshot_date 必须是 YYYY-MM-DD。")
    core_root = core_root.resolve(strict=True)
    if not core_root.is_dir():
        raise BuildError(f"核心领域根不是目录：{core_root}")
    target_root = _ensure_inside(target_root or core_root / TARGET_DIRECTORY, core_root, "目标目录")
    if not target_root.is_dir():
        raise BuildError(f"目标转投目录不存在：{target_root}")
    staging_root = _ensure_inside(staging_root, core_root, "staging 目录")
    if staging_root.exists():
        raise BuildError(f"staging 目录已存在，请使用新目录：{staging_root}")
    staging_root.mkdir(parents=True)
    payload_root = staging_root / "payload"
    payload_root.mkdir()

    records = _load_records(paper_records)
    evidence_by_id, evidence_ignored = _load_evidence(evidence_dir)
    audits = _load_code_audits(code_audits)
    expectations = dict(DEFAULT_EXPECTED_COUNTS)
    input_digests = {
        "paper_records": _sha256_file(paper_records),
        "evidence_set": _sha256_bytes(
            _json_bytes(
                {
                    key: _sha256_file(Path(value["_source_path"]))
                    for key, value in sorted(evidence_by_id.items())
                }
            )
        ),
        "code_audits": _sha256_bytes(
            _json_bytes({str(path.resolve()): _sha256_file(path) for path in code_audits})
        ),
        "expectations": _sha256_bytes(_json_bytes(expectations)),
    }
    build_id = _sha256_bytes(_json_bytes({"snapshot_date": snapshot_date, **input_digests}))[:20]
    blockers: list[str] = []
    warnings: list[str] = [f"ignored_non_evidence_json:{path}" for path in evidence_ignored]
    domain_records: dict[str, list[dict[str, Any]]] = {key: [] for key in DOMAIN_LABELS}

    for domain in DOMAIN_LABELS:
        actual = sum(1 for record in records if record["_reclassified_domain"] == domain)
        if actual != expectations[domain]:
            blockers.append(f"count_mismatch:{domain}:{actual}!={expectations[domain]}")
    if len(records) != sum(expectations.values()):
        blockers.append(f"total_count_mismatch:{len(records)}!={sum(expectations.values())}")

    for record in records:
        paper_id = str(record["paper_id"])
        domain = str(record["_reclassified_domain"])
        final_dir = _ensure_inside(target_root / domain / paper_id, target_root, "论文目录")
        if not final_dir.is_dir():
            blockers.append(f"paper_directory_missing:{domain}:{paper_id}")
        target_pdf = final_dir / "paper.pdf"
        declared_target = _text(record.get("target_pdf_path"))
        if declared_target and Path(declared_target).resolve(strict=False) != target_pdf:
            blockers.append(f"target_pdf_path_conflict:{domain}:{paper_id}")
        try:
            facts = _pdf_facts(target_pdf)
        except BuildError as exc:
            blockers.append(f"pdf_invalid:{domain}:{paper_id}:{exc}")
            raw_pdf = record.get("pdf")
            pdf: Mapping[str, Any] = raw_pdf if isinstance(raw_pdf, Mapping) else {}
            fallback_size = pdf.get("size")
            fallback_pages = pdf.get("page_count")
            facts = PdfFacts(
                target_pdf,
                _text(pdf.get("sha256")) or "0" * 64,
                fallback_size if isinstance(fallback_size, int) else 0,
                fallback_pages if isinstance(fallback_pages, int) else 0,
            )
        raw_declared_pdf = record.get("pdf")
        declared_pdf: Mapping[str, Any] = (
            raw_declared_pdf if isinstance(raw_declared_pdf, Mapping) else {}
        )
        if _text(declared_pdf.get("sha256")) != facts.sha256:
            blockers.append(f"pdf_sha256_mismatch:{domain}:{paper_id}")
        if declared_pdf.get("size") != facts.size:
            blockers.append(f"pdf_size_mismatch:{domain}:{paper_id}")
        if declared_pdf.get("page_count") != facts.pages:
            blockers.append(f"pdf_page_count_mismatch:{domain}:{paper_id}")

        evidence = evidence_by_id.get(paper_id)
        if evidence is None:
            blockers.append(f"analysis_evidence_missing:{paper_id}")
            evidence_output: dict[str, Any] = {
                "schema_version": "paper-analysis-evidence.v1",
                "status": "error",
                "paper": {"paper_id": paper_id},
                "error": {"type": "missing", "message": "待核验：未提供逐页证据包。"},
                "evidence": {},
            }
        else:
            evidence_output, evidence_blockers = _validated_evidence(evidence, facts, paper_id)
            blockers.extend(evidence_blockers)
        analysis, overview = _analysis_from_evidence(record, evidence_output)

        final_html = final_dir / "paper.html"
        declared_html = _text(record.get("target_html_path"))
        if declared_html and Path(declared_html).resolve(strict=False) != final_html:
            blockers.append(f"target_html_path_conflict:{domain}:{paper_id}")
        declared_metadata = _text(record.get("target_metadata_path"))
        if declared_metadata and Path(declared_metadata).resolve(strict=False) != (
            final_dir / "metadata.json"
        ):
            blockers.append(f"target_metadata_path_conflict:{domain}:{paper_id}")
        code_metadata, html_code, code_blockers, code_warnings = _code_for_record(
            record, audits.get(paper_id), core_root, final_html
        )
        blockers.extend(code_blockers)
        warnings.extend(code_warnings)

        stage_dir = payload_root / domain / paper_id
        _write_json(stage_dir / "analysis-evidence.json", evidence_output)
        _write_bytes(stage_dir / "analysis.zh.md", overview.encode("utf-8"))
        html_record = {
            "paper_id": paper_id,
            "title": record.get("title"),
            "title_zh": record.get("title_zh"),
            "authors": record.get("authors"),
            "year": record.get("year"),
            "venue": record.get("venue"),
            "doi": record.get("doi"),
            "arxiv_id": record.get("arxiv_id"),
            "abstract": record.get("abstract"),
            "summary": record.get("summary")
            or "本页基于已核验摘要与逐页原文证据整理；缺少证据的项目保持待核验。",
            "tags": record.get("tags") or [],
            "domain_slugs": [domain],
            "source_urls": record.get("source_urls") or [],
            "pdf": {
                "path": "paper.pdf",
                "sha256": facts.sha256,
                "size": facts.size,
                "page_count": facts.pages,
                "version": declared_pdf.get("version"),
            },
            "analysis": analysis,
            "code": html_code,
            "decision_evidence": record.get("decision_evidence") or [],
            "warnings": record.get("warnings") or [],
        }
        warnings.extend(_render_paper_html(html_record, stage_dir / "paper.html", core_root))
        html_sha = _sha256_file(stage_dir / "paper.html")
        metadata = {
            "schema_version": METADATA_SCHEMA,
            "snapshot_date": snapshot_date,
            "build_id": build_id,
            "paper_id": paper_id,
            "source_record_id": record.get("source_record_id") or paper_id,
            "canonical_paper_id": record.get("canonical_paper_id") or paper_id,
            "title": record.get("title"),
            "title_zh": record.get("title_zh"),
            "authors": record.get("authors"),
            "year": record.get("year"),
            "venue": record.get("venue"),
            "doi": record.get("doi"),
            "arxiv_id": record.get("arxiv_id"),
            "abstract": record.get("abstract"),
            "summary": record.get("summary"),
            "tags": record.get("tags") or [],
            "domain_slug": domain,
            "source_group": record.get("source_group"),
            "disposition": record.get("disposition"),
            "pdf": {
                "path": "paper.pdf",
                "sha256": facts.sha256,
                "size": facts.size,
                "page_count": facts.pages,
                "version": declared_pdf.get("version"),
            },
            "html": {"path": "paper.html", "sha256": html_sha},
            "analysis": {
                "evidence_path": "analysis-evidence.json",
                "evidence_sha256": _sha256_file(stage_dir / "analysis-evidence.json"),
                "overview_path": "analysis.zh.md",
                "overview_sha256": _sha256_file(stage_dir / "analysis.zh.md"),
                "coverage": {key: len(value) for key, value in analysis.items()},
            },
            "code": code_metadata,
            "source_urls": record.get("source_urls") or [],
            "decision_evidence": record.get("decision_evidence") or [],
            "warnings": record.get("warnings") or [],
            "no_guide": True,
            "input_digests": dict(input_digests),
        }
        _write_json(stage_dir / "metadata.json", metadata)
        domain_records[domain].append(metadata)

    for domain, values in domain_records.items():
        values.sort(key=lambda item: str(item["paper_id"]))
        catalog = {
            "schema_version": CATALOG_SCHEMA,
            "domain": domain,
            "label": DOMAIN_LABELS[domain],
            "snapshot_date": snapshot_date,
            "build_id": build_id,
            "record_count": len(values),
            "records": values,
            "no_guide": True,
        }
        _write_json(payload_root / domain / "catalog.json", catalog)
        _write_bytes(
            payload_root / domain / "index.html",
            _domain_index(domain, values, snapshot_date).encode("utf-8"),
        )
        _write_bytes(
            payload_root / domain / "README.md",
            _domain_readme(domain, len(values), snapshot_date).encode("utf-8"),
        )

    artifacts = [
        _artifact(path, payload_root) for path in sorted(payload_root.rglob("*")) if path.is_file()
    ]
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "snapshot_date": snapshot_date,
        "build_id": build_id,
        "record_count": len(records),
        "domain_counts": {key: len(value) for key, value in domain_records.items()},
        "input_digests": input_digests,
        "html_engine": "tools/paper_intake_html/paper_html.py",
        "artifacts": artifacts,
        "owned_paths": sorted([item["path"] for item in artifacts] + [MANIFEST_NAME]),
        "no_guide": True,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }
    manifest["manifest_payload_sha256"] = _sha256_bytes(_json_bytes(manifest))
    _write_json(payload_root / MANIFEST_NAME, manifest)
    planned_without_manifest = sorted(
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    )
    planned = [*planned_without_manifest, MANIFEST_NAME]
    status = "ready" if not blockers else "staged_with_blockers"
    created = updated = unchanged = 0
    if apply:
        if blockers:
            raise BuildError("apply 被硬门禁阻断：\n" + "\n".join(sorted(set(blockers))[:100]))
        created, updated, unchanged = _apply_payload(payload_root, target_root, core_root, planned)
        status = "applied"
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "mode": "apply" if apply else "dry-run-staging",
        "build_id": build_id,
        "staging_root": str(staging_root),
        "target_root": str(target_root),
        "record_count": len(records),
        "domain_counts": {key: len(value) for key, value in domain_records.items()},
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "planned_files": len(planned),
        "no_guide": True,
        "apply_counts": {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
        },
    }
    _write_json(staging_root / "build-report.json", report)
    return BuildResult(
        status=status,
        build_id=build_id,
        staging_root=staging_root,
        target_root=target_root,
        counts={key: len(value) for key, value in domain_records.items()},
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
        applied=apply,
        files_created=created,
        files_updated=updated,
        files_unchanged=unchanged,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为217份转投论文生成元数据、证据中文概述、paper HTML 与领域目录。"
    )
    parser.add_argument("--paper-records", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--code-audit", type=Path, action="append", required=True)
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="事务提交 staging；默认只构建 staging，官方公开代码未本地归档时硬阻断。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_reclassified_papers(
            paper_records=args.paper_records,
            evidence_dir=args.evidence_dir,
            code_audits=args.code_audit,
            core_root=args.core_root,
            target_root=args.target_root,
            staging_root=args.staging_root,
            snapshot_date=args.snapshot_date,
            apply=args.apply,
        )
    except Exception as exc:
        print(f"构建失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "build_id": result.build_id,
                "staging_root": str(result.staging_root),
                "target_root": str(result.target_root),
                "counts": dict(result.counts),
                "blockers": list(result.blockers),
                "warnings": list(result.warnings),
                "applied": result.applied,
                "files_created": result.files_created,
                "files_updated": result.files_updated,
                "files_unchanged": result.files_unchanged,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
