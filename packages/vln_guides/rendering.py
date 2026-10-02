# ruff: noqa: E501
"""Build self-contained, accessible HTML guides from VLN extraction records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json, atomic_write_text

from .audit import _content_errors

GUIDE_VERSION = "vln-unified-guide/v1"
AUTHORING_SCHEMA = "research-asset-hub.vln-guide-content/v1"
QUEUE_SCHEMA = "research-asset-hub.vln-guide-authoring-queue/v1"

GLOSSARY: dict[str, str] = {
    "VLN": "视觉语言导航：智能体根据自然语言指令和视觉观察，在环境中移动到目标位置。",
    "VLM": "视觉语言模型：共同处理图像与文本的模型。",
    "MLLM": "多模态大语言模型：能联合理解文字、图像或视频的大模型。",
    "SR": "成功率：最终到达目标范围内的轨迹比例。",
    "SPL": "成功加权路径长度：同时考虑是否成功和路径是否高效。",
    "nDTW": "归一化动态时间规整：衡量预测路径与参考路径整体形状的接近程度。",
    "R2R": "Room-to-Room：室内离散视觉语言导航的经典数据集。",
    "RxR": "Room-across-Room：覆盖多语言指令的室内视觉语言导航数据集。",
    "Habitat": "常用于具身导航研究的三维仿真平台。",
    "CoT": "思维链：把中间推理步骤显式组织起来的推理方式。",
    "zero-shot": "零样本：不针对目标任务额外训练或微调，直接使用模型完成任务。",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _paragraphs(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [text for item in value if (text := _clean(item))]
    text = _clean(value)
    return [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]


def _strip_markdown(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return value.lstrip("> ").strip()




def build_authoring_queue(
    inventory: Mapping[str, Any],
    *,
    extraction_root: Path,
    output_path: Path,
    authored_root: Path | None = None,
) -> dict[str, Any]:
    queue: list[dict[str, Any]] = []
    authored_ids = (
        set(_load_authored_contents(authored_root))
        if authored_root is not None
        else set()
    )
    for raw_row in inventory.get("items", []):
        if not isinstance(raw_row, Mapping):
            continue
        asset_id = str(raw_row["asset_id"])
        if asset_id in authored_ids:
            continue
        extraction_path = extraction_root / asset_id / "extraction.json"
        extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        relevant_sections = [
            {
                "kind": item.get("kind"),
                "heading": item.get("heading"),
                "page": item.get("page"),
                "excerpt": _clean(item.get("excerpt"))[:2_500],
            }
            for item in extraction.get("sections", [])
            if isinstance(item, Mapping)
            and str(item.get("kind", "")).startswith(
                ("abstract", "introduction", "method", "approach", "experiment", "result", "conclusion", "limitation", "dataset", "benchmark")
            )
        ][:12]
        table_summaries = [
            {
                "page": item.get("page"),
                "rows": item.get("rows", [])[:8],
            }
            for item in extraction.get("tables", [])
            if isinstance(item, Mapping) and item.get("status") == "extracted"
        ][:4]
        queue.append(
            {
                "asset_id": asset_id,
                "title": raw_row.get("title"),
                "title_zh": raw_row.get("title_zh"),
                "authors": raw_row.get("authors", []),
                "year": raw_row.get("year"),
                "venue": raw_row.get("venue"),
                "abstract": raw_row.get("abstract"),
                "summary": raw_row.get("summary"),
                "tags": raw_row.get("tags", []),
                "identifiers": raw_row.get("identifiers", []),
                "page_count": extraction.get("pdf", {}).get("page_count"),
                "sections": relevant_sections,
                "metric_evidence": extraction.get("metric_evidence", [])[:12],
                "table_summaries": table_summaries,
                "extraction_status": extraction.get("status"),
                "extraction_warnings": extraction.get("warnings", []),
                "existing_documents": extraction.get("existing_documents", []),
            }
        )
    report = {
        "schema": QUEUE_SCHEMA,
        "status": "ready",
        "requires_authored_content": len(queue),
        "items": queue,
    }
    atomic_write_json(output_path, report)
    return report


def _load_authored_contents(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.is_dir():
        return result
    for candidate in sorted(path.glob("*.json")):
        raw_payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, dict):
            raise TypeError(f"authoring payload must be an object in {candidate}")
        raw_items = raw_payload.get("items", [raw_payload])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise TypeError(f"authoring items must be a sequence in {candidate}")
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise TypeError(f"authoring item must be an object in {candidate}")
            payload: dict[str, Any] = raw_item
            if payload.get("schema") != AUTHORING_SCHEMA:
                raise ValueError(f"unexpected authoring schema in {candidate}")
            asset_id = str(payload.get("asset_id") or "")
            if not asset_id:
                raise ValueError(f"authoring asset_id is missing in {candidate}")
            if asset_id in result:
                raise ValueError(f"duplicate authored content for {asset_id}")
            result[asset_id] = payload
    return result


def _glossary(row: Mapping[str, Any], content: Mapping[str, Any]) -> list[dict[str, str]]:
    explicit = content.get("glossary", [])
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        values = [
            {"term": _clean(item.get("term")), "definition": _clean(item.get("definition"))}
            for item in explicit
            if isinstance(item, Mapping)
            and _clean(item.get("term"))
            and _clean(item.get("definition"))
        ]
        if values:
            return values
    haystack = " ".join(
        [
            _clean(row.get("title")),
            _clean(row.get("abstract")),
            json.dumps(content, ensure_ascii=False),
        ]
    )
    return [
        {"term": term, "definition": definition}
        for term, definition in GLOSSARY.items()
        if term.casefold() in haystack.casefold()
    ][:8]


BASE_CSS = """
:root{color-scheme:light;--bg:#f4f1e9;--paper:#fffdfa;--ink:#17211d;--muted:#5d6b64;
--line:#d9ddd5;--green:#17624f;--green-soft:#e2f0ea;--blue:#245f78;--blue-soft:#e7f1f5;
--amber:#8a5a12;--amber-soft:#fff2d8;--shadow:0 18px 50px rgba(24,51,41,.09);font-synthesis:none}
*{box-sizing:border-box}:where(h1,h2,h3,p,li,dd,.fact){overflow-wrap:anywhere}
html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Segoe UI","Microsoft YaHei UI","PingFang SC",Arial,sans-serif;line-height:1.75}
a{color:var(--green);text-underline-offset:.18em}.skip{position:absolute;left:-9999px;top:8px;background:#fff;
padding:8px 12px;z-index:20}.skip:focus{left:8px}.hero{background:linear-gradient(135deg,#123e34,#1d6d58);
color:#fff;padding:52px max(24px,calc((100vw - 1120px)/2)) 44px}.eyebrow{font-size:.78rem;font-weight:750;
letter-spacing:.12em;text-transform:uppercase;opacity:.82}.hero h1{font-size:clamp(2rem,4vw,3.55rem);
line-height:1.12;max-width:1000px;margin:.55rem 0 1rem}.hero .zh{font-size:1.16rem;opacity:.9;margin:.3rem 0}
.meta{max-width:900px;opacity:.84}.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.chip{border:1px solid
rgba(255,255,255,.32);border-radius:999px;padding:4px 10px;font-size:.82rem;background:rgba(255,255,255,.08)}
.layout{max-width:1120px;margin:28px auto 64px;padding:0 20px;display:grid;grid-template-columns:minmax(0,1fr)
260px;gap:28px}.toc{position:sticky;top:20px;align-self:start;background:var(--paper);border:1px solid var(--line);
border-radius:18px;padding:18px;box-shadow:var(--shadow)}.toc strong{display:block;margin-bottom:8px}.toc a{display:block;
padding:5px 0;text-decoration:none}.toc .fine{font-size:.78rem;color:var(--muted);margin-top:14px}.paper{min-width:0}
.section{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:clamp(22px,4vw,38px);
margin-bottom:22px;box-shadow:var(--shadow)}.section h2{font-size:clamp(1.35rem,2.5vw,1.85rem);line-height:1.3;
margin:0 0 16px}.section h3{margin:1.4em 0 .4em}.lead{font-size:1.1rem}.callout{border-left:5px solid var(--green);
background:var(--green-soft);padding:16px 18px;border-radius:12px;margin:16px 0}.warning{border-left-color:var(--amber);
background:var(--amber-soft)}.evidence{border-left-color:var(--blue);background:var(--blue-soft)}.steps{display:grid;gap:12px;
counter-reset:step}.step{counter-increment:step;border:1px solid var(--line);border-radius:15px;padding:16px 18px}.step h3{margin:0 0 6px}
.step h3:before{content:counter(step) ". ";color:var(--green)}ul,ol{padding-left:1.35rem}.facts{display:grid;
grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.fact{border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.fact b{display:block;font-size:.75rem;letter-spacing:.04em;color:var(--muted)}.table-wrap{overflow-x:auto}.terms{display:grid;
grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.term{border:1px solid var(--line);border-radius:14px;padding:14px}
.term dt{font-weight:800;color:var(--green)}.term dd{margin:4px 0 0}.basis{font-size:.78rem;color:var(--muted)}details{border-top:1px
solid var(--line);padding-top:12px;margin-top:16px}summary{cursor:pointer;font-weight:700}.sources a{overflow-wrap:anywhere}
footer{max-width:1120px;margin:0 auto 32px;padding:0 20px;color:var(--muted);font-size:.85rem}
@media(max-width:850px){.layout{grid-template-columns:1fr}.toc{position:static}.hero{padding-top:38px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media(max-width:480px){.layout{padding:0 12px}.section{border-radius:16px;padding:20px}.hero{padding-left:18px;
padding-right:18px}.hero h1{font-size:2rem}}
@media print{body{background:#fff}.hero{background:#fff;color:#000;padding:20px 0}.layout{display:block;margin:0;max-width:none}
.toc{display:none}.section{box-shadow:none;break-inside:avoid}.chip{border-color:#777}footer{max-width:none}}
"""


def _p(items: object, *, css_class: str = "") -> str:
    class_attribute = f' class="{escape(css_class)}"' if css_class else ""
    return "".join(
        f"<p{class_attribute}>{escape(item)}</p>" for item in _paragraphs(items)
    )


def _ul(items: object) -> str:
    values = _paragraphs(items)
    if not values:
        return ""
    return "<ul>" + "".join(f"<li>{escape(_strip_markdown(item))}</li>" for item in values) + "</ul>"


def _external_links(row: Mapping[str, Any]) -> str:
    links: list[str] = []
    for raw_url in row.get("source_urls", []):
        url = _clean(raw_url)
        if not url.startswith(("https://", "http://")):
            continue
        host = urlparse(url).hostname or "来源"
        label = f"项目/出版页面：{host}（联网）"
        if "arxiv.org" in url:
            label = "arXiv PDF（联网）" if "/pdf/" in url else "arXiv 论文页（联网）"
        if "github.com" in url:
            label = "代码（联网）"
        links.append(
            f'<li><a href="{escape(url, quote=True)}" rel="noopener noreferrer">'
            f"{escape(label)}</a></li>"
        )
    return "".join(links) or "<li>未登记外部来源链接。</li>"


def _identifiers(row: Mapping[str, Any]) -> str:
    values = [
        f"{_clean(item.get('scheme')).upper()}: {_clean(item.get('value'))}"
        for item in row.get("identifiers", [])
        if isinstance(item, Mapping)
        and _clean(item.get("scheme")) in {"arxiv", "doi"}
    ]
    return " · ".join(values) or "无强标识"


def _release_label(row: Mapping[str, Any], key: str) -> str:
    attributes = row.get("attributes", {})
    release = attributes.get("release_status", {}) if isinstance(attributes, Mapping) else {}
    value = str(release.get(key, "unknown")) if isinstance(release, Mapping) else "unknown"
    labels = {
        "official_open": "官方代码已开源",
        "official_announced": "官方代码已宣布",
        "third_party_only": "仅第三方实现",
        "unavailable": "未发现代码",
        "open": "Checkpoint 已公开",
        "announced": "Checkpoint 已宣布",
        "not_found": "未发现 Checkpoint",
        "not_applicable": "不适用 Checkpoint",
        "unknown": "状态待核验",
    }
    return labels.get(value, value)


def render_guide_html(
    row: Mapping[str, Any],
    extraction: Mapping[str, Any],
    content: Mapping[str, Any],
) -> str:
    title = _clean(row.get("title"))
    title_zh = _clean(row.get("title_zh"))
    authors = "、".join(_clean(item) for item in row.get("authors", []) if _clean(item))
    year = _clean(row.get("year")) or "年份未知"
    venue = _clean(row.get("venue")) or "发表渠道待核验"
    page_count = int(extraction.get("pdf", {}).get("page_count") or 0)
    source_kind = _clean(content.get("content_source"))
    is_draft = source_kind == "unreviewed_draft"
    kind = str(content.get("content_kind") or "method")
    problem_title, evidence_title, limits_title = {
        "survey": ("这篇综述如何界定问题", "分类框架与证据", "覆盖范围与研究空白"),
        "dataset": ("数据要解决什么问题", "数据规模与评测结果", "数据偏差与使用范围"),
        "benchmark": ("基准测试什么能力", "评测设置与结果", "指标边界与局限"),
        "system": ("系统要解决什么问题", "部署与实验结果", "部署条件与局限"),
    }.get(kind, ("问题为什么难", "实验结果与证据", "适用范围与局限"))
    approach_title = _clean(content.get("approach_title")) or "方法怎么工作"
    steps = content.get("steps", [])
    evidence = content.get("evidence", [])
    limitations = content.get("limitations", [])
    glossary = _glossary(row, content)
    abstract = _clean(row.get("abstract"))
    extraction_warning = "；".join(
        _clean(item) for item in extraction.get("warnings", []) if _clean(item)
    )

    step_html = "".join(
        '<div class="step"><h3>'
        + escape(_clean(item.get("title")) or "方法要点")
        + "</h3>"
        + _p(item.get("body"))
        + "</div>"
        for item in steps
        if isinstance(item, Mapping) and _clean(item.get("body"))
    )
    if not step_html:
        step_html = '<div class="callout warning">方法步骤尚未完成通俗化复核，请对照原 PDF。</div>'

    evidence_html = "".join(
        '<li><span>'
        + escape(_clean(item.get("claim")))
        + "</span>"
        + (
            f' <span class="basis">PDF p.{int(item["page"])}</span>'
            if item.get("page")
            else f' <span class="basis">{escape(_clean(item.get("basis")) or "来源记录")}</span>'
        )
        + "</li>"
        for item in evidence
        if isinstance(item, Mapping) and _clean(item.get("claim"))
    )
    if not evidence_html:
        evidence_html = "<li>尚未形成可发布的定量证据摘要；请直接核对 PDF 的实验章节。</li>"

    limitation_html = "".join(
        '<li><span>'
        + escape(_clean(item.get("text")))
        + "</span>"
        + (
            f' <span class="basis">PDF p.{int(item["page"])}</span>'
            if item.get("page")
            else f' <span class="basis">{escape(_clean(item.get("basis")) or "保守判断")}</span>'
        )
        + "</li>"
        for item in limitations
        if isinstance(item, Mapping) and _clean(item.get("text"))
    )
    if not limitation_html:
        limitation_html = "<li>原文未提供可直接抽取的局限段落；需结合实验范围人工判断。</li>"

    glossary_html = "".join(
        '<div class="term"><dt>'
        + escape(item["term"])
        + "</dt><dd>"
        + escape(item["definition"])
        + "</dd></div>"
        for item in glossary
    )
    release_code = _release_label(row, "code_open")
    release_checkpoint = _release_label(row, "checkpoint_open")
    draft_chip = '<span class="chip">待人工通俗化复核</span>' if is_draft else ""
    title_zh_html = f'<p class="zh">{escape(title_zh)}</p>' if title_zh else ""
    warning_html = (
        f'<div class="callout warning">抽取警告：{escape(extraction_warning)}</div>'
        if extraction_warning
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN" data-guide-version="{GUIDE_VERSION}" data-content-kind="{escape(kind)}" data-content-source="{escape(source_kind)}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{escape(title)} · 通俗导读</title>
<style>{BASE_CSS}</style></head><body>
<a class="skip" href="#main">跳到正文</a>
<header class="hero"><div class="eyebrow">VLN Paper Guide · {escape(year)} · {escape(venue)}</div>
<h1>{escape(title)}</h1>{title_zh_html}<p class="meta">{escape(authors or '作者信息待核验')}</p>
<div class="chips"><span class="chip">{page_count} 页 PDF</span><span class="chip">{escape(release_code)}</span>
<span class="chip">{escape(release_checkpoint)}</span>{draft_chip}</div></header>
<div class="layout"><main id="main" class="paper"><article>
<section class="section" id="takeaway"><h2>一分钟看懂</h2>{_p(content.get('one_minute'), css_class='lead')}
{('<div class="callout warning">这是自动结构化草稿，尚未达到最终发布标准。</div>' if is_draft else '')}</section>
<section class="section" id="problem"><h2>{escape(problem_title)}</h2>{_p(content.get('problem'))}
{_ul(content.get('problem_points'))}</section>
<section class="section" id="approach"><h2>{escape(approach_title)}</h2>
{_p(content.get('approach_intro'))}<div class="steps">{step_html}</div></section>
<section class="section" id="evidence"><h2>{escape(evidence_title)}</h2>
<ul>{evidence_html}</ul>{_ul(content.get('evidence_notes'))}</section>
<section class="section" id="limits"><h2>{escape(limits_title)}</h2><ul>{limitation_html}</ul></section>
<section class="section" id="terms"><h2>术语速查</h2><dl class="terms">{glossary_html}</dl></section>
<section class="section sources" id="sources"><h2>来源与原文核对</h2>
<p>{escape(_identifiers(row))} · 本地 PDF 共 {page_count} 页。证据中的页码按本地 PDF 页面顺序计数。</p>
<details><summary>版本、提取与复核记录</summary>
<p>PDF SHA-256：{escape(str(extraction.get('pdf', {}).get('sha256', '')))}</p>
<p>本地文字与表格提取：{escape(str(extraction.get('extractor', {}).get('version', 'unknown')))}。公式及复杂表格请对照原 PDF。</p>
{warning_html}{_ul(content.get('review', {}).get('notes', []))}</details>
<ul>{_external_links(row)}</ul><details><summary>查看英文摘要</summary><p>{escape(abstract or '无摘要')}</p></details></section>
</article></main><nav class="toc" aria-label="本文目录"><strong>本文目录</strong>
<a href="#takeaway">一分钟看懂</a><a href="#problem">{escape(problem_title)}</a><a href="#approach">{escape(approach_title)}</a>
<a href="#evidence">{escape(evidence_title)}</a><a href="#limits">{escape(limits_title)}</a><a href="#terms">术语速查</a>
<a href="#sources">来源与原文核对</a><p class="fine">中文导读 · 可离线阅读</p></nav></div>
<footer>Research Asset Hub · VLN 统一通俗导读 · {GUIDE_VERSION}</footer></body></html>"""


def render_inventory(
    inventory: Mapping[str, Any],
    *,
    extraction_root: Path,
    authored_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]
    registry = SourceRegistry.load(project_root / "config" / "sources.yaml")
    if any(root.root_key != "source" and root.path.resolve() == output_root.resolve()
           for root in registry.describe()):
        raise ValueError("Output is a registered read-only source. Use a new version directory.")
    papers_root = output_root / "papers"
    papers_root.mkdir(parents=True, exist_ok=True)
    authored_contents = _load_authored_contents(authored_root)
    expected = int(inventory.get("expected_paper_count") or inventory.get("paper_count") or 0)
    expected_ids = {str(row["asset_id"]) for row in inventory["items"]}
    if set(authored_contents) != expected_ids:
        raise ValueError(
            f"Authored content coverage mismatch: missing={len(expected_ids - set(authored_contents))}, "
            f"extra={len(set(authored_contents) - expected_ids)}"
        )
    items: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    for raw_row in inventory.get("items", []):
        if not isinstance(raw_row, Mapping):
            continue
        asset_id = str(raw_row["asset_id"])
        extraction_path = extraction_root / asset_id / "extraction.json"
        extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        content = dict(authored_contents[asset_id])
        content.setdefault("glossary", _glossary(raw_row, content))
        errors = _content_errors(content, extraction, require_review=True)
        if errors:
            raise ValueError(f"Guide {asset_id} is not ready: {', '.join(errors)}")
        content["provenance"] = {
            "pdf_sha256": extraction["pdf"]["sha256"],
            "extraction_sha256": hashlib.sha256(extraction_path.read_bytes()).hexdigest(),
            "extractor": extraction["extractor"],
            "existing_documents": [
                {key: doc[key] for key in ("representation_id", "sha256", "type")}
                for doc in extraction["existing_documents"]
            ],
        }
        content_source = str(content.get("content_source") or "unknown")
        source_counts[content_source] = source_counts.get(content_source, 0) + 1
        paper_root = papers_root / asset_id
        paper_root.mkdir(parents=True, exist_ok=True)
        html_value = render_guide_html(raw_row, extraction, content)
        html_path = paper_root / "guide.html"
        content_path = paper_root / "content.json"
        atomic_write_text(html_path, html_value)
        atomic_write_json(content_path, dict(content))
        items.append(
            {
                "asset_id": asset_id,
                "title": raw_row.get("title"),
                "content_source": content_source,
                "html_path": f"papers/{asset_id}/guide.html",
                "content_path": f"papers/{asset_id}/content.json",
                "html_sha256": _sha256_text(html_value),
            }
        )
    items.sort(key=lambda item: str(item["asset_id"]))
    report = {
        "schema": "research-asset-hub.vln-unified-guide-render/v1",
        "status": (
            "passed"
            if len(items) == expected and source_counts.get("unreviewed_draft", 0) == 0
            else "draft"
        ),
        "guide_version": GUIDE_VERSION,
        "expected": expected,
        "rendered": len(items),
        "content_sources": dict(sorted(source_counts.items())),
        "items": items,
    }
    atomic_write_json(output_root / "render-report.json", report)
    return report
