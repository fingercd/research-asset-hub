"""Static, database-independent rendering for curated domain portals."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from packages.domain.exporters.common import atomic_write_json, atomic_write_text
from packages.domain_portals.schemas import DomainPortalDocument


def _text(value: object) -> str:
    return html.escape(str(value)) if value is not None else ""


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _markdown_as_paragraphs(value: object) -> str:
    paragraphs = [
        item.strip().lstrip("#").strip()
        for item in str(value or "").split("\n\n")
        if item.strip()
    ]
    return "".join(f"<p>{_text(item)}</p>" for item in paragraphs)


def _section_html(identifier: str, content: dict[str, Any]) -> str:
    if identifier in {"scope", "frontiers"}:
        return _markdown_as_paragraphs(content.get("markdown"))
    if identifier == "timeline":
        return "<ol class=\"timeline\">" + "".join(
            (
                f"<li><strong>{_text(item.get('period'))}</strong>"
                f"<div><h3>{_text(item.get('title'))}</h3>"
                f"<p>{_text(item.get('summary'))}</p></div></li>"
            )
            for item in _records(content.get("stages"))
        ) + "</ol>"
    if identifier == "capability-method-map":
        return "<div class=\"grid\">" + "".join(
            (
                f"<article><h3>{_text(item.get('title'))}</h3>"
                f"<p>{_text(item.get('summary'))}</p></article>"
            )
            for item in _records(content.get("capabilities"))
        ) + "</div>"
    if identifier == "landmarks":
        return "<div class=\"grid landmarks\">" + "".join(
            (
                f"<article><h3>{_text(item.get('title'))}</h3>"
                f"<p>{_text(item.get('summary'))}</p></article>"
            )
            for item in _records(content.get("items"))
        ) + "</div>"
    if identifier == "benchmarks":
        rows = _records(content.get("rows"))
        if not rows:
            return f"<p>{_text(content.get('not_applicable_reason'))}</p>"
        body = "".join(
            (
                f"<tr><th>{_text(item.get('task'))}</th>"
                f"<td>{_text('、'.join(item.get('datasets_or_environments') or []))}</td>"
                f"<td>{_text('、'.join(item.get('metrics') or []))}</td>"
                f"<td>{_text(item.get('caution'))}</td></tr>"
            )
            for item in rows
        )
        return (
            "<div class=\"table\"><table><thead><tr><th>任务</th><th>数据集 / 环境</th>"
            f"<th>指标</th><th>注意事项</th></tr></thead><tbody>{body}</tbody></table></div>"
        )
    if identifier == "reading-routes":
        return "<div class=\"grid\">" + "".join(
            (
                f"<article><h3>{_text(route.get('title'))}</h3>"
                f"<p>{_text(route.get('goal'))}</p><ol>"
                + "".join(
                    f"<li>{_text(step.get('title'))} — {_text(step.get('rationale'))}</li>"
                    for step in _records(route.get("steps"))
                )
                + "</ol></article>"
            )
            for route in _records(content.get("routes"))
        ) + "</div>"
    if identifier == "catalog":
        return f"<p>{_text(content.get('summary'))}</p>"
    if identifier == "evidence":
        return (
            f"<p>已登记 {_text(len(_records(content.get('source_inputs'))))} 个来源输入；"
            "统一页面为派生视图，原始资料保持只读。</p>"
        )
    return f"<pre>{_text(json.dumps(content, ensure_ascii=False, indent=2))}</pre>"


def _render_html(portal: DomainPortalDocument) -> str:
    sections = "\n".join(
        (
            f'<section id="{_text(section.id)}"><p class="eyebrow">{_text(section.id)}</p>'
            f"<h2>{_text(section.title)}</h2>"
            f"{_section_html(section.id, section.content)}</section>"
        )
        for section in portal.sections
    )
    navigation = "".join(
        f'<a href="#{_text(section.id)}">{_text(section.title)}</a>'
        for section in portal.sections
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_text(portal.label)} · Research Asset Hub</title>
<style>
:root{{--ink:#17201c;--muted:#66716b;--line:#dce1dc;--accent:#246b53;--soft:#f4f5f2}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;color:var(--ink);
background:var(--soft);font:15px/1.7 Inter,"Segoe UI","Microsoft YaHei UI",sans-serif}}
main{{width:min(1180px,100%);margin:auto;padding:48px 32px 90px}}header{{padding:40px 0;
border-bottom:1px solid var(--line)}}h1{{margin:0 0 12px;font-size:clamp(38px,6vw,72px);
line-height:1.05;letter-spacing:-.05em}}header p{{max-width:820px;color:var(--muted)}}
nav{{position:sticky;top:0;display:flex;gap:18px;padding:14px 0;overflow:auto;
border-bottom:1px solid var(--line);background:rgba(244,245,242,.94);backdrop-filter:blur(10px)}}
nav a{{flex:0 0 auto;color:var(--accent);font-size:12px;text-decoration:none}}
section{{padding:72px 0;border-bottom:1px solid var(--line)}}h2{{margin:0 0 28px;
font-size:clamp(28px,4vw,44px);letter-spacing:-.035em}}h3{{margin:0 0 8px}}
.eyebrow{{margin:0 0 5px;color:var(--accent);font-size:11px;font-weight:800;
letter-spacing:.12em;text-transform:uppercase}}.timeline{{list-style:none;padding:0}}
.timeline li{{display:grid;grid-template-columns:180px 1fr;gap:32px;padding:22px 0;
border-top:1px solid var(--line)}}.timeline strong{{color:var(--accent);font-size:24px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
.grid article{{padding:22px;border:1px solid var(--line);border-radius:10px;background:#fff}}
.landmarks{{grid-template-columns:repeat(3,minmax(0,1fr))}}.table{{overflow:auto}}
table{{width:100%;min-width:720px;border-collapse:collapse;background:#fff}}
th,td{{padding:13px;border:1px solid var(--line);text-align:left;vertical-align:top}}
@media(max-width:680px){{main{{padding:28px 18px 70px}}.grid,.landmarks{{grid-template-columns:1fr}}
.timeline li{{grid-template-columns:1fr;gap:8px}}}}
</style>
</head>
<body><main><header><p class="eyebrow">{_text(portal.slug)}</p>
<h1>{_text(portal.label)}</h1><p>{_text(portal.description)}</p></header>
<nav aria-label="领域目录">{navigation}</nav>{sections}</main></body></html>"""


def export_domain_portal(
    portal: DomainPortalDocument,
    destination: Path,
) -> list[Path]:
    """Write portal JSON and a semantic standalone HTML page."""

    return [
        atomic_write_json(
            destination / "portal.json",
            portal.model_dump(mode="json", by_alias=True),
        ),
        atomic_write_text(destination / "index.html", _render_html(portal)),
    ]
