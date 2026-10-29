"""Human-readable Markdown/Obsidian catalog export."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..schemas import NormalizedRecord
from .common import atomic_write_text, jsonable


def _yaml_scalar(value: Any) -> str:
    # JSON strings are valid YAML scalars and avoid a PyYAML runtime dependency.
    return json.dumps(jsonable(value), ensure_ascii=False)


def _escape_cell(value: Any) -> str:
    return str(value).replace("|", r"\|").replace("\n", " ")


def _state_by_asset(values: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        asset_id = str(value.get("asset_id", ""))
        if asset_id:
            result[asset_id] = value
    return result


def _annotations_by_asset(
    values: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in values:
        asset_id = str(value.get("asset_id", ""))
        if asset_id:
            result[asset_id].append(value)
    return result


def _render_record(
    record: NormalizedRecord,
    reading_state: dict[str, Any] | None,
    annotations: list[dict[str, Any]],
) -> str:
    asset = record.asset
    lines = [
        "---",
        "schema: research-asset-hub.asset/v1",
        f"asset_id: {_yaml_scalar(asset.asset_id)}",
        f"asset_type: {_yaml_scalar(asset.asset_type)}",
        f"title: {_yaml_scalar(asset.title)}",
        f"source_id: {_yaml_scalar(record.source_id)}",
        f"source_record_id: {_yaml_scalar(record.source_record_id)}",
        f"domains: {_yaml_scalar(asset.domain_slugs)}",
        f"tags: {_yaml_scalar(asset.tags)}",
        f"visibility: {_yaml_scalar(asset.visibility)}",
    ]
    if asset.year is not None:
        lines.append(f"year: {asset.year}")
    if reading_state:
        lines.append(f"reading_status: {_yaml_scalar(reading_state.get('status'))}")
    lines.extend(["---", "", f"# {asset.title}", ""])

    if asset.title_zh and asset.title_zh != asset.title:
        lines.extend([f"**中文标题：** {asset.title_zh}", ""])
    if asset.authors:
        lines.extend([f"**作者：** {', '.join(asset.authors)}", ""])
    if asset.venue:
        lines.extend([f"**发表：** {asset.venue}", ""])
    if asset.abstract:
        lines.extend(["## 摘要", "", asset.abstract, ""])
    elif asset.summary:
        lines.extend(["## 摘要", "", asset.summary, ""])

    lines.extend(["## 外部标识", ""])
    if asset.external_identifiers:
        for identifier in asset.external_identifiers:
            lines.append(f"- `{identifier.scheme}:{identifier.canonical}`")
    else:
        lines.append("- 无")
    lines.append("")

    lines.extend(
        [
            "## 表示",
            "",
            "| 类型 | 角色 | 位置 | SHA-256 |",
            "|---|---|---|---|",
        ]
    )
    if record.representations:
        for representation in record.representations:
            if representation.path_ref:
                location = (
                    f"{representation.source_id or record.source_id}:"
                    f"{representation.root_key}/{representation.path_ref}"
                )
            else:
                location = str(representation.url)
            lines.append(
                "| "
                + " | ".join(
                    _escape_cell(value)
                    for value in (
                        representation.representation_type.value,
                        representation.role.value,
                        location,
                        representation.sha256 or "",
                    )
                )
                + " |"
            )
    else:
        lines.append("| 无 |  |  |  |")
    lines.append("")

    lines.extend(["## 关系", ""])
    if record.relations:
        for relation in record.relations:
            target = relation.object.model_dump(mode="json", exclude_none=True)
            lines.append(
                f"- **{relation.relation_type.value}** → "
                f"`{json.dumps(target, ensure_ascii=False)}` "
                f"（来源：{relation.evidence.source}）"
            )
    else:
        lines.append("- 无")
    lines.append("")

    lines.extend(["## 我的批注", ""])
    if annotations:
        for annotation in annotations:
            title = annotation.get("title") or annotation.get("kind") or "批注"
            body = annotation.get("body_markdown") or annotation.get("body") or ""
            lines.extend([f"### {title}", "", str(body), ""])
    else:
        lines.extend(["无", ""])

    lines.extend(
        [
            "## 来源",
            "",
            f"- 来源：`{record.source_id}`",
            f"- 原记录 ID：`{record.source_record_id}`",
            f"- 适配器：`{record.adapter_name}@{record.adapter_version}`",
            "",
        ]
    )
    return "\n".join(lines)


def export_markdown(
    records: Iterable[NormalizedRecord],
    destination: str | Path,
    *,
    reading_states: Iterable[dict[str, Any]] = (),
    annotations: Iterable[dict[str, Any]] = (),
) -> list[Path]:
    """Write one stable, Obsidian-friendly Markdown file per asset."""

    destination_path = Path(destination)
    state_lookup = _state_by_asset(reading_states)
    annotation_lookup = _annotations_by_asset(annotations)
    written: list[Path] = []
    for record in records:
        asset_id = str(record.asset.asset_id)
        path = destination_path / f"{asset_id}.md"
        written.append(
            atomic_write_text(
                path,
                _render_record(
                    record,
                    state_lookup.get(asset_id),
                    annotation_lookup.get(asset_id, []),
                ),
            )
        )
    return written
