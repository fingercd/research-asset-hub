"""Build the immutable 2026-08-31 portal additions sidecar.

This intentionally leaves the frozen 2026-07-26 ten-portal snapshot untouched.
All four entrypoints must already exist below the core-domain root; missing or
escaping paths fail closed before any output is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from packages.adapters.base import stable_uuid7
from packages.adapters.canonical_snapshot import (
    CanonicalFieldPointer,
    CanonicalSnapshotDocument,
    CanonicalSnapshotInput,
    CanonicalSnapshotItem,
)
from packages.domain.enums import (
    MaterializationKind,
    RepresentationAvailability,
    RepresentationRole,
    RepresentationType,
)
from packages.domain.exporters.common import atomic_write_json
from packages.domain.schemas import (
    NormalizedAsset,
    RepresentationCandidate,
    RepresentationMountCandidate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT.parent.resolve()
SOURCE_ID = "domain-portals-20260831"
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"


@dataclass(frozen=True, slots=True)
class MountSpec:
    virtual_path: str
    path_ref: str


@dataclass(frozen=True, slots=True)
class PortalSpec:
    slug: str
    label: str
    description: str
    root_key: str
    asset_root: str
    entrypoint: str
    mounts: tuple[MountSpec, ...]


PORTALS: tuple[PortalSpec, ...] = (
    PortalSpec(
        slug="image-editing",
        label="图像编辑",
        description="指令式图像编辑、多轮保真、区域控制与 DiT/流匹配推理加速。",
        root_key="image-editing-portal",
        asset_root="图像编辑",
        entrypoint="index.html",
        mounts=(
            MountSpec("/outputs", "outputs"),
            MountSpec("/outputs/pdfs", "outputs/pdfs"),
            MountSpec("/outputs/papers_html", "outputs/papers_html"),
            MountSpec("/outputs/code", "outputs/code"),
        ),
    ),
    PortalSpec(
        slug="video-llm-compression",
        label="视频大模型压缩",
        description="面向 VideoLLM 的帧选择、视觉 token 压缩、KV/记忆管理与流式推理。",
        root_key="video-llm-compression-portal",
        asset_root="VLM框架3DV压缩",
        entrypoint="index.v2.html",
        mounts=(
            MountSpec("/library/site", "library/site"),
            MountSpec("/library/papers", "library/papers"),
            MountSpec("/library/code", "library/code"),
            MountSpec("/outputs", "outputs"),
        ),
    ),
    PortalSpec(
        slug="embodied-ai-compression",
        label="具身智能压缩",
        description="面向具身智能体与 VLA 的视觉、记忆、规划和行动表示压缩。",
        root_key="embodied-ai-compression-portal",
        asset_root="具身智能压缩",
        entrypoint="index.html",
        mounts=(
            MountSpec("/library/site", "library/site"),
            MountSpec("/library/papers", "library/papers"),
            MountSpec("/library/code", "library/code"),
            MountSpec("/outputs", "outputs"),
        ),
    ),
    PortalSpec(
        slug="navigation-compression",
        label="导航压缩",
        description="面向 VLN 与具身导航的地图、观测、历史记忆与路径计算压缩。",
        root_key="navigation-compression-portal",
        asset_root="导航压缩",
        entrypoint="index.html",
        mounts=(
            MountSpec("/library/site", "library/site"),
            MountSpec("/library/papers", "library/papers"),
            MountSpec("/library/code", "library/code"),
            MountSpec("/outputs", "outputs"),
        ),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_path(spec: PortalSpec) -> Path:
    entry = (CORE_ROOT / spec.asset_root / spec.entrypoint).resolve(strict=True)
    try:
        entry.relative_to(CORE_ROOT)
    except ValueError as error:
        raise ValueError(f"portal entry escapes core root: {entry}") from error
    if not entry.is_file():
        raise FileNotFoundError(f"domain overview entry is missing: {entry}")
    return entry


def build_snapshot() -> CanonicalSnapshotDocument:
    inputs: list[CanonicalSnapshotInput] = []
    items: list[CanonicalSnapshotItem] = []
    for index, spec in enumerate(PORTALS):
        entry = _entry_path(spec)
        source_path = entry.relative_to(CORE_ROOT).as_posix()
        digest = _sha256(entry)
        size_bytes = entry.stat().st_size
        inputs.append(
            CanonicalSnapshotInput(
                path=source_path,
                sha256=digest,
                size_bytes=size_bytes,
            )
        )
        asset = NormalizedAsset(
            asset_id=stable_uuid7(SOURCE_ID, f"overview:{spec.slug}"),
            asset_type="collection",
            title=f"{spec.label}领域总览",
            title_zh=f"{spec.label}领域总览",
            authors=[],
            summary=spec.description,
            analysis={},
            source_urls=[],
            domain_slugs=[spec.slug],
            tags=["domain-overview"],
            attributes={
                "kind": "domain-overview",
                "system_hidden": True,
                "domain_label": spec.label,
                "description": spec.description,
                "default_presentation": "hub",
                "portal_source_id": SOURCE_ID,
            },
        )
        mounts = [
            RepresentationMountCandidate(
                mount_path="/",
                source_id=SOURCE_ID,
                root_key=spec.root_key,
                path_ref=spec.entrypoint,
                priority=0,
            ),
            *[
                RepresentationMountCandidate(
                    mount_path=mount.virtual_path,
                    source_id=SOURCE_ID,
                    root_key=spec.root_key,
                    path_ref=mount.path_ref,
                    priority=10,
                )
                for mount in spec.mounts
            ],
        ]
        representation = RepresentationCandidate(
            representation_type=RepresentationType.HTML,
            role=RepresentationRole.PRIMARY,
            materialization_kind=MaterializationKind.FILE,
            availability=RepresentationAvailability.AVAILABLE,
            source_id=SOURCE_ID,
            root_key=spec.root_key,
            path_ref=spec.entrypoint,
            entrypoint=spec.entrypoint,
            sha256=digest,
            mime_type="text/html",
            size_bytes=size_bytes,
            metadata={
                "kind": "domain-overview",
                "domain_slug": spec.slug,
                "strict_mounts": True,
                "source_entry": source_path,
                "theme_version": "research-theme-v2",
            },
            mounts=mounts,
        )
        pointers = {
            field: CanonicalFieldPointer(json_pointer=f"/items/{index}/asset/{field}")
            for field in (
                "title",
                "title_zh",
                "summary",
                "domain_slugs",
                "tags",
                "attributes",
            )
        }
        items.append(
            CanonicalSnapshotItem(
                source_record_id=f"overview:{spec.slug}",
                asset=asset,
                field_pointers=pointers,
                representations=[representation],
                source_payload={
                    "overview_entry": source_path,
                    "overview_sha256": digest,
                    "asset_root": spec.asset_root,
                },
            )
        )
    return CanonicalSnapshotDocument(
        source_id=SOURCE_ID,
        profile="domain_portals",
        domain_slugs=[spec.slug for spec in PORTALS],
        inputs=inputs,
        items=items,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    output = arguments.output.expanduser().resolve()
    allowed = (PROJECT_ROOT / "config" / "intake" / SOURCE_ID).resolve()
    try:
        output.relative_to(allowed)
    except ValueError as error:
        raise ValueError(f"portal snapshot output must stay below {allowed}") from error
    snapshot = build_snapshot()
    atomic_write_json(output, snapshot)
    print(
        json.dumps(
            {
                "status": "written",
                "source_id": SOURCE_ID,
                "records": len(snapshot.items),
                "domains": snapshot.domain_slugs,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
