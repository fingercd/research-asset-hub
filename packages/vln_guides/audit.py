"""Static quality audit for generated VLN unified guides."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from packages.domain.exporters.common import atomic_write_json

REQUIRED_SECTIONS = frozenset(
    {"takeaway", "problem", "approach", "evidence", "limits", "terms", "sources"}
)
FORBIDDEN_TAGS = frozenset({"script", "iframe", "form", "object", "embed"})
WINDOWS_PATH_PATTERN = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")


class _GuideParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.duplicate_ids: list[str] = []
        self.unsafe_attributes: list[str] = []
        self.anchor_targets: list[str] = []
        self.tags: list[str] = []
        self.active_resources: list[str] = []
        self.lang: str | None = None
        self.main_count = 0
        self.article_count = 0
        self.h1_count = 0
        self.viewport = False
        self.text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append(tag)
        values = {name: value or "" for name, value in attrs}
        if tag == "html":
            self.lang = values.get("lang")
        if tag == "main":
            self.main_count += 1
        if tag == "article":
            self.article_count += 1
        if tag == "h1":
            self.h1_count += 1
        if values.get("id"):
            if values["id"] in self.ids:
                self.duplicate_ids.append(values["id"])
            self.ids.add(values["id"])
        for key, value in values.items():
            if key.startswith("on") or (key == "href" and value.lower().startswith("javascript:")):
                self.unsafe_attributes.append(f"{tag}.{key}")
        if tag == "a" and values.get("href", "").startswith("#"):
            self.anchor_targets.append(values["href"][1:])
        if tag == "meta" and values.get("name", "").casefold() == "viewport":
            self.viewport = True
        for attribute in ("src", "srcset", "poster"):
            if values.get(attribute):
                self.active_resources.append(f"{tag}.{attribute}:{values[attribute]}")
        if tag == "link" and values.get("href"):
            self.active_resources.append(f"link.href:{values['href']}")

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _sequence(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _content_errors(
    content: Mapping[str, Any],
    extraction: Mapping[str, Any] | None = None,
    *,
    require_review: bool = False,
) -> list[str]:
    errors: list[str] = []
    if content.get("schema") != "research-asset-hub.vln-guide-content/v1":
        errors.append("invalid_content_schema")
    if content.get("content_source") == "unreviewed_draft":
        errors.append("unreviewed_draft")
    elif content.get("content_source") != "manual_synthesis_from_pdf_and_metadata":
        errors.append("content_not_rewritten")
    requirements = {
        "one_minute": 1,
        "problem": 1,
        "steps": 1,
        "evidence": 1,
        "limitations": 1,
        "glossary": 1,
    }
    for field, minimum in requirements.items():
        if len(_sequence(content.get(field))) < minimum:
            errors.append(f"content_field_too_short:{field}")
    if not str(content.get("approach_title") or "").strip():
        errors.append("missing_approach_title")
    if len(_sequence(content.get("steps"))) < 3:
        errors.append("insufficient_method_steps")
    for field, text_key in (
        ("steps", "body"),
        ("evidence", "claim"),
        ("limitations", "text"),
        ("glossary", "definition"),
    ):
        for item in _sequence(content.get(field)):
            if not isinstance(item, Mapping) or not str(item.get(text_key) or "").strip():
                errors.append(f"empty_content_item:{field}")
    if extraction is not None:
        page_count = int(extraction["pdf"]["page_count"])
        for field in ("evidence", "limitations"):
            for item in _sequence(content.get(field)):
                if not isinstance(item, Mapping):
                    continue
                page = item.get("page")
                if page is not None and (
                    not isinstance(page, int)
                    or isinstance(page, bool)
                    or not 1 <= page <= page_count
                ):
                    errors.append(f"invalid_page:{field}")
                if not str(item.get("basis") or "").strip():
                    errors.append(f"missing_basis:{field}")
                if field == "evidence" and page is None:
                    errors.append("evidence_missing_page")
    if require_review:
        review = content.get("review")
        if not isinstance(review, Mapping) or review.get("status") != "reviewed":
            errors.append("missing_review")
        elif extraction is not None:
            if review.get("pdf_sha256") != extraction["pdf"]["sha256"]:
                errors.append("review_pdf_mismatch")
            known_ids = {item["representation_id"] for item in extraction["existing_documents"]}
            reviewed_ids = set(review.get("source_documents", []))
            if reviewed_ids - known_ids:
                errors.append("review_unknown_document")
            if known_ids and not reviewed_ids:
                errors.append("legacy_documents_not_reviewed")
            if not review.get("notes"):
                errors.append("review_notes_missing")
    return errors


def _html_errors(value: str) -> tuple[list[str], dict[str, Any]]:
    parser = _GuideParser()
    parser.feed(value)
    parser.close()
    errors: list[str] = []
    if not value.lstrip().casefold().startswith("<!doctype html>"):
        errors.append("missing_doctype")
    if parser.lang != "zh-CN":
        errors.append("invalid_or_missing_lang")
    if not parser.viewport:
        errors.append("missing_viewport")
    if parser.main_count != 1:
        errors.append("main_count")
    if parser.article_count != 1:
        errors.append("article_count")
    if parser.h1_count != 1:
        errors.append("h1_count")
    missing_sections = sorted(REQUIRED_SECTIONS - parser.ids)
    if missing_sections:
        errors.append(f"missing_sections:{','.join(missing_sections)}")
    missing_anchors = sorted(
        {target for target in parser.anchor_targets if target and target not in parser.ids}
    )
    if missing_anchors:
        errors.append(f"broken_anchors:{','.join(missing_anchors)}")
    forbidden = sorted(FORBIDDEN_TAGS.intersection(parser.tags))
    if forbidden:
        errors.append(f"forbidden_tags:{','.join(forbidden)}")
    if parser.active_resources:
        errors.append("active_resources")
    if parser.duplicate_ids:
        errors.append("duplicate_ids")
    if parser.unsafe_attributes:
        errors.append("unsafe_attributes")
    if re.search(r"@import|url\s*\(", value, re.IGNORECASE):
        errors.append("css_resource_request")
    forbidden_fragments = {
        "parent_traversal": "../",
        "file_url": "file://",
        "localhost": "localhost",
        "loopback": "127.0.0.1",
        "draft_marker": "待人工通俗化复核",
        "draft_warning": "尚未达到最终发布标准",
    }
    for name, fragment in forbidden_fragments.items():
        if fragment in value:
            errors.append(name)
    if WINDOWS_PATH_PATTERN.search(value):
        errors.append("windows_absolute_path")
    visible_text = re.sub(r"\s+", "", "".join(parser.text))
    chinese_characters = len(re.findall(r"[\u3400-\u9fff]", visible_text))
    if chinese_characters < 180:
        errors.append("insufficient_chinese_explanation")
    return errors, {
        "ids": len(parser.ids),
        "internal_anchors": len(parser.anchor_targets),
        "chinese_characters": chinese_characters,
        "active_resources": parser.active_resources,
    }


def audit_guides(
    inventory: Mapping[str, Any],
    *,
    output_root: Path,
    report_path: Path,
    excluded_asset_ids: set[str] | None = None,
    extraction_root: Path | None = None,
) -> dict[str, Any]:
    expected = {
        str(item["asset_id"]): str(item["title"])
        for item in inventory.get("items", [])
        if isinstance(item, Mapping)
    }
    papers_root = output_root / "papers"
    actual = (
        {path.name for path in papers_root.iterdir() if path.is_dir()}
        if papers_root.is_dir()
        else set()
    )
    excluded_present = sorted(actual.intersection(excluded_asset_ids or set()))
    manifest_path = output_root / "render-report.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    hashes = {item["asset_id"]: item["html_sha256"] for item in manifest.get("items", [])}
    items: list[dict[str, Any]] = []
    for asset_id, title in sorted(expected.items()):
        root = papers_root / asset_id
        html_path = root / "guide.html"
        content_path = root / "content.json"
        errors: list[str] = []
        details: dict[str, Any] = {}
        if not html_path.is_file():
            errors.append("missing_html")
        if not content_path.is_file():
            errors.append("missing_content")
        if not errors:
            raw_content = json.loads(content_path.read_text(encoding="utf-8"))
            if not isinstance(raw_content, Mapping):
                errors.append("content_not_object")
            else:
                if str(raw_content.get("asset_id")) != asset_id:
                    errors.append("content_asset_id_mismatch")
                extraction = (
                    json.loads(
                        (extraction_root / asset_id / "extraction.json").read_text(encoding="utf-8")
                    )
                    if extraction_root
                    else None
                )
                errors.extend(
                    _content_errors(raw_content, extraction, require_review=bool(extraction_root))
                )
            html_value = html_path.read_text(encoding="utf-8")
            if extraction_root and hashlib.sha256(html_path.read_bytes()).hexdigest() != hashes.get(
                asset_id
            ):
                errors.append("render_hash_mismatch")
            if extraction_root and isinstance(raw_content, Mapping):
                provenance = raw_content.get("provenance", {})
                expected_digest = hashlib.sha256(
                    (extraction_root / asset_id / "extraction.json").read_bytes()
                ).hexdigest()
                if provenance.get("extraction_sha256") != expected_digest:
                    errors.append("extraction_provenance_mismatch")
            html_errors, details = _html_errors(html_value)
            errors.extend(html_errors)
            if escape(title) not in html_value:
                errors.append("title_mismatch")
        items.append(
            {
                "asset_id": asset_id,
                "title": title,
                "status": "passed" if not errors else "failed",
                "errors": sorted(set(errors)),
                "details": details,
            }
        )
    stale = sorted(actual - set(expected))
    failed = [item for item in items if item["status"] == "failed"]
    report = {
        "schema": "research-asset-hub.vln-unified-guide-audit/v1",
        "status": (
            "passed"
            if len(expected) == int(inventory.get("expected_paper_count") or 0)
            and not failed
            and not stale
            and not excluded_present
            else "failed"
        ),
        "expected": len(expected),
        "found": len(actual),
        "passed": len(items) - len(failed),
        "failed": len(failed),
        "missing": sorted(set(expected) - actual),
        "stale": stale,
        "excluded_asset_ids_present": excluded_present,
        "items": items,
    }
    atomic_write_json(report_path, report)
    return report
