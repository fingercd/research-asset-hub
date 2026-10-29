from __future__ import annotations

import json
from pathlib import Path

from packages.vln_guides.audit import audit_guides
from packages.vln_guides.rendering import AUTHORING_SCHEMA, render_guide_html


def _row() -> dict[str, object]:
    return {
        "asset_id": "11111111-1111-1111-1111-111111111111",
        "title": "A Ground Robot VLN Paper",
        "title_zh": "一篇地面机器人视觉语言导航论文",
        "authors": ["Researcher"],
        "year": 2026,
        "venue": "Test",
        "abstract": "A sufficiently long abstract about indoor navigation.",
        "source_urls": ["https://arxiv.org/abs/2601.00001"],
        "identifiers": [{"scheme": "arxiv", "value": "2601.00001"}],
        "attributes": {},
    }


def _extraction() -> dict[str, object]:
    return {
        "pdf": {"page_count": 10, "sha256": "a" * 64},
        "text": {"characters": 20000},
        "extractor": {"version": "test/v1"},
        "warnings": [],
    }


def _content() -> dict[str, object]:
    return {
        "schema": AUTHORING_SCHEMA,
        "asset_id": "11111111-1111-1111-1111-111111111111",
        "content_source": "manual_synthesis_from_pdf_and_metadata",
        "one_minute": ["这篇论文研究地面机器人如何根据语言和视觉完成室内导航。"],
        "problem": ["长程指令需要持续记住任务进度，并把语言对应到当前观察。"],
        "approach_intro": ["方法把观察、记忆与动作规划组织成闭环。"],
        "approach_title": "方法怎么工作",
        "steps": [
            {"title": "理解指令", "body": "提取目标、方向和地标。"},
            {"title": "更新记忆", "body": "保存与当前路线有关的观察。"},
            {"title": "选择动作", "body": "结合当前状态执行下一步。"},
        ],
        "evidence": [{"claim": "在未见场景中进行评测。", "basis": "PDF", "page": 7}],
        "limitations": [{"text": "只覆盖室内场景。", "basis": "实验范围", "page": 8}],
        "glossary": [{"term": "VLN", "definition": "视觉语言导航。"}],
    }


def test_audit_accepts_a_complete_self_contained_guide(tmp_path: Path) -> None:
    row = _row()
    content = _content()
    root = tmp_path / "guides" / "papers" / str(row["asset_id"])
    root.mkdir(parents=True)
    (root / "guide.html").write_text(
        render_guide_html(row, _extraction(), content),
        encoding="utf-8",
    )
    (root / "content.json").write_text(
        json.dumps(content, ensure_ascii=False),
        encoding="utf-8",
    )
    report = audit_guides(
        {"expected_paper_count": 1, "items": [row]},
        output_root=tmp_path / "guides",
        report_path=tmp_path / "audit.json",
    )

    assert report["status"] == "passed"
    assert report["passed"] == 1


def test_audit_rejects_draft_and_active_resources(tmp_path: Path) -> None:
    row = _row()
    content = _content()
    content["content_source"] = "unreviewed_draft"
    root = tmp_path / "guides" / "papers" / str(row["asset_id"])
    root.mkdir(parents=True)
    html = render_guide_html(row, _extraction(), content).replace(
        "</head>",
        '<script src="https://example.invalid/app.js"></script></head>',
    )
    (root / "guide.html").write_text(html, encoding="utf-8")
    (root / "content.json").write_text(json.dumps(content), encoding="utf-8")
    report = audit_guides(
        {"expected_paper_count": 1, "items": [row]},
        output_root=tmp_path / "guides",
        report_path=tmp_path / "audit.json",
    )

    assert report["status"] == "failed"
    errors = report["items"][0]["errors"]
    assert "unreviewed_draft" in errors
    assert "active_resources" in errors
    assert "forbidden_tags:script" in errors
