"""Load and validate one domain portal from the versioned configuration tree."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schemas import (
    BenchmarkDocument,
    CapabilityMethodDocument,
    DomainPortalDocument,
    LandmarkDocument,
    PortalIndex,
    PortalMetadata,
    PortalSection,
    PortalStaleReason,
    ReadingRouteDocument,
    TimelineDocument,
)

SECTION_TITLES = {
    "scope": "领域定位与边界",
    "timeline": "发展时间线",
    "capability-method-map": "能力与方法版图",
    "landmarks": "代表工作与技术路线",
    "benchmarks": "任务、数据集与评测",
    "reading-routes": "推荐阅读路线",
    "catalog": "论文、代码与导读目录",
    "frontiers": "前沿趋势与开放问题",
    "evidence": "证据、来源与版本说明",
}


class DomainPortalLoadError(ValueError):
    """Raised when a curated portal cannot be validated safely."""


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise DomainPortalLoadError(f"cannot read portal YAML {path}: {error}") from error


def _read_markdown(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise DomainPortalLoadError(f"cannot read portal Markdown {path}: {error}") from error
    if not value:
        raise DomainPortalLoadError(f"portal Markdown cannot be empty: {path}")
    return value


def _digest_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix().casefold()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_portal_index(config_root: Path) -> PortalIndex:
    path = config_root / "portal-index.yaml"
    try:
        return PortalIndex.model_validate(_read_yaml(path))
    except ValidationError as error:
        raise DomainPortalLoadError(f"invalid portal index {path}: {error}") from error


def _stale_reasons(metadata: PortalMetadata, source_root: Path) -> list[PortalStaleReason]:
    reasons: list[PortalStaleReason] = []
    for source_input in metadata.source_inputs:
        path = (source_root / source_input.path).resolve(strict=False)
        try:
            path.relative_to(source_root.resolve())
        except ValueError:
            reasons.append(
                PortalStaleReason(path=source_input.path, reason="missing")
            )
            continue
        if not path.is_file():
            reasons.append(
                PortalStaleReason(path=source_input.path, reason="missing")
            )
            continue
        stat = path.stat()
        if stat.st_size != source_input.size_bytes:
            reasons.append(
                PortalStaleReason(
                    path=source_input.path,
                    reason="size_changed",
                    expected=source_input.size_bytes,
                    actual=stat.st_size,
                )
            )
        digest = _sha256(path)
        if digest != source_input.sha256:
            reasons.append(
                PortalStaleReason(
                    path=source_input.path,
                    reason="digest_changed",
                    expected=source_input.sha256,
                    actual=digest,
                )
            )
        if stat.st_mtime_ns != source_input.mtime_ns:
            reasons.append(
                PortalStaleReason(
                    path=source_input.path,
                    reason="mtime_changed",
                    expected=source_input.mtime_ns,
                    actual=stat.st_mtime_ns,
                )
            )
    return reasons


def _section(
    identifier: str,
    kind: str,
    status: str,
    content: dict[str, object],
) -> PortalSection:
    return PortalSection.model_validate(
        {
            "id": identifier,
            "kind": kind,
            "title": SECTION_TITLES[identifier],
            "status": status,
            "content": content,
        }
    )


def load_domain_portal(
    config_root: Path,
    source_root: Path,
    slug: str,
) -> DomainPortalDocument:
    index = load_portal_index(config_root)
    registry = next((item for item in index.portals if item.slug == slug), None)
    if registry is None:
        raise DomainPortalLoadError(f"domain portal is not registered: {slug}")
    directory = (config_root / registry.content_directory).resolve()
    try:
        directory.relative_to(config_root.resolve())
    except ValueError as error:
        raise DomainPortalLoadError("portal content directory escapes config root") from error
    required = {
        "portal": directory / "portal.yaml",
        "overview": directory / "overview.md",
        "timeline": directory / "timeline.yaml",
        "map": directory / "capability-method-map.yaml",
        "landmarks": directory / "landmarks.yaml",
        "routes": directory / "reading-routes.yaml",
        "benchmarks": directory / "benchmarks.yaml",
        "frontiers": directory / "open-questions.md",
    }
    missing = [path for path in required.values() if not path.is_file()]
    if missing:
        raise DomainPortalLoadError(
            "portal content is incomplete: " + ", ".join(str(item) for item in missing)
        )
    try:
        metadata = PortalMetadata.model_validate(_read_yaml(required["portal"]))
        timeline = TimelineDocument.model_validate(_read_yaml(required["timeline"]))
        capability_map = CapabilityMethodDocument.model_validate(
            _read_yaml(required["map"])
        )
        landmarks = LandmarkDocument.model_validate(_read_yaml(required["landmarks"]))
        routes = ReadingRouteDocument.model_validate(_read_yaml(required["routes"]))
        benchmarks = BenchmarkDocument.model_validate(
            _read_yaml(required["benchmarks"])
        )
    except ValidationError as error:
        raise DomainPortalLoadError(f"invalid portal content for {slug}: {error}") from error
    if metadata.slug != slug or metadata.slug != registry.slug:
        raise DomainPortalLoadError("portal slug does not match its registry entry")
    if metadata.original_selector != registry.original_selector:
        raise DomainPortalLoadError("portal original selector does not match registry")

    stale_reasons = _stale_reasons(metadata, source_root)
    status = "needs_review" if stale_reasons else metadata.review_status
    overview = _read_markdown(required["overview"])
    frontiers = _read_markdown(required["frontiers"])
    evidence_status = status
    sections = [
        _section(
            "scope",
            "scope",
            status,
            {
                "markdown": overview,
                "asset_ids": metadata.section_evidence.get("scope", []),
            },
        ),
        _section(
            "timeline",
            "timeline",
            status,
            timeline.model_dump(mode="json"),
        ),
        _section(
            "capability-method-map",
            "capability_method_map",
            status,
            capability_map.model_dump(mode="json"),
        ),
        _section(
            "landmarks",
            "landmarks",
            status,
            landmarks.model_dump(mode="json"),
        ),
        _section(
            "benchmarks",
            "benchmarks",
            benchmarks.status if not stale_reasons else "needs_review",
            benchmarks.model_dump(mode="json"),
        ),
        _section(
            "reading-routes",
            "reading_routes",
            status,
            routes.model_dump(mode="json"),
        ),
        _section(
            "catalog",
            "catalog",
            status,
            {
                "summary": "完整研究单元由正式库动态生成，统一版只保存入口语义。",
                "asset_ids": metadata.section_evidence.get("catalog", []),
            },
        ),
        _section(
            "frontiers",
            "frontiers",
            status,
            {
                "markdown": frontiers,
                "asset_ids": metadata.section_evidence.get("frontiers", []),
            },
        ),
        _section(
            "evidence",
            "evidence",
            evidence_status,
            {
                "source_inputs": [
                    item.model_dump(mode="json") for item in metadata.source_inputs
                ],
                "original_selector": metadata.original_selector.model_dump(mode="json"),
                "appendices": metadata.appendices,
            },
        ),
    ]
    content_digest = _digest_files(list(required.values()))
    try:
        return DomainPortalDocument.model_validate(
            {
                "schema": "research-asset-hub.domain-portal/v2",
                "slug": slug,
                "label": metadata.label,
                "description": metadata.description,
                "content_version": metadata.content_version,
                "content_digest": content_digest,
                "review_status": status,
                "stale": bool(stale_reasons),
                "stale_reasons": [
                    item.model_dump(mode="json") for item in stale_reasons
                ],
                "original_selector": metadata.original_selector.model_dump(mode="json"),
                "sections": [item.model_dump(mode="json") for item in sections],
            }
        )
    except ValidationError as error:
        payload = json.dumps(
            {"slug": slug, "error_count": error.error_count()},
            ensure_ascii=False,
        )
        raise DomainPortalLoadError(f"assembled portal is invalid: {payload}") from error
