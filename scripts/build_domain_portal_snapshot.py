"""Build the immutable, multi-domain overview sidecar without changing source HTML."""

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
CORE_ROOT = PROJECT_ROOT.parent
SOURCE_ID = "domain-portals-20260726"
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"


@dataclass(frozen=True, slots=True)
class MountSpec:
    virtual_path: str
    root_key: str
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


def _mount(virtual_path: str, root_key: str, path_ref: str) -> MountSpec:
    return MountSpec(virtual_path=virtual_path, root_key=root_key, path_ref=path_ref)


PORTALS: tuple[PortalSpec, ...] = (
    PortalSpec(
        slug="agent",
        label="Agent 学习",
        description="Agent 方法、规划、工具使用、记忆与多智能体研究资料。",
        root_key="agent-portal",
        asset_root="agent 学习/outputs/agent-paper-library/site",
        entrypoint="index.html",
        mounts=tuple(
            _mount(f"/{name}", "agent-portal", name)
            for name in ("assets", "timeline", "modules", "reading", "evidence", "papers")
        ),
    ),
    PortalSpec(
        slug="infra",
        label="AI / 大模型 Infra",
        description="训练、推理、并行、显存、算子和大模型系统基础设施。",
        root_key="infra-portal",
        asset_root="infra/outputs/infra_paper_library",
        entrypoint="index.html",
        mounts=tuple(
            _mount(f"/{name}", "infra-portal", name)
            for name in ("assets", "papers", "code", "pdfs")
        ),
    ),
    PortalSpec(
        slug="world-model",
        label="世界模型",
        description="基于预测、潜变量、视频生成和规划的世界模型研究。",
        root_key="world-model-portal",
        asset_root="世界模型",
        entrypoint="index.html",
        mounts=(
            _mount("/library/site", "world-model-portal", "library/site"),
            _mount("/library/papers", "world-model-portal", "library/papers"),
            _mount("/outputs", "world-model-portal", "outputs"),
        ),
    ),
    PortalSpec(
        slug="reinforcement-learning",
        label="强化学习",
        description="强化学习基础、离线与在线学习、对齐和决策研究。",
        root_key="reinforcement-learning-portal",
        asset_root="强化学习/outputs/强化学习论文研究站",
        entrypoint="index.html",
        mounts=tuple(
            _mount(f"/{name}", "reinforcement-learning-portal", name)
            for name in ("assets", "papers", "code", "data")
        ),
    ),
    PortalSpec(
        slug="fine-tuning",
        label="大模型微调",
        description="参数高效微调、持续预训练、偏好优化和后训练方法。",
        root_key="fine-tuning-portal",
        asset_root="微调/outputs/fine_tuning_library/site",
        entrypoint="index.html",
        mounts=tuple(
            _mount(f"/{name}", "fine-tuning-portal", name)
            for name in ("assets", "papers")
        ),
    ),
    PortalSpec(
        slug="generation",
        label="图像与视频生成",
        description="图像、视频、扩散、流匹配和多模态生成研究。",
        root_key="generation-portal",
        asset_root="生成/outputs/paper_library/site",
        entrypoint="index.html",
        mounts=tuple(
            _mount(f"/{name}", "generation-portal", name)
            for name in ("assets", "pages")
        ),
    ),
    PortalSpec(
        slug="llm-tech-reports",
        label="大语言模型与大厂技术报告",
        description="大模型技术报告、模型卡、系统与训练方法资料。",
        root_key="llm-tech-reports-portal",
        asset_root="大厂技术报告/outputs/llm-tech-report-library",
        entrypoint="index.html",
        mounts=tuple(
            _mount(f"/{name}", "llm-tech-reports-portal", name)
            for name in ("assets", "data", "reports", "pdfs", "code")
        ),
    ),
    PortalSpec(
        slug="vision-language-navigation",
        label="视觉语言导航（VLN）",
        description="视觉语言导航、具身导航数据集、方法和评测。",
        root_key="vln-portal",
        asset_root="VLN",
        entrypoint="index.html",
        mounts=(
            _mount("/library/site", "vln-portal", "library/site"),
            _mount("/library/papers", "vln-portal", "library/papers"),
            _mount("/outputs", "vln-portal", "outputs"),
        ),
    ),
    PortalSpec(
        slug="game-rl",
        label="游戏强化学习",
        description="棋类、电子游戏、多智能体博弈和通用游戏智能。",
        root_key="game-rl-portal",
        asset_root="游戏RL",
        entrypoint="index.html",
        mounts=(
            _mount("/library/site", "game-rl-portal", "library/site"),
            _mount("/library/papers", "game-rl-portal", "library/papers"),
            _mount("/outputs", "game-rl-portal", "outputs"),
        ),
    ),
    PortalSpec(
        slug="wsvad",
        label="弱监督视频异常检测（WSVAD）",
        description="弱监督视频异常检测、时序定位、视觉语言模型与评测。",
        root_key="wsvad-portal",
        asset_root="VAD",
        entrypoint="index.html",
        mounts=(
            _mount("/library/site", "wsvad-portal", "library/site"),
            _mount("/library/papers", "wsvad-portal", "library/papers"),
            _mount("/outputs", "wsvad-portal", "outputs"),
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
    return (CORE_ROOT / spec.asset_root / spec.entrypoint).resolve()


def build_snapshot() -> CanonicalSnapshotDocument:
    inputs: list[CanonicalSnapshotInput] = []
    items: list[CanonicalSnapshotItem] = []
    for index, spec in enumerate(PORTALS):
        entry = _entry_path(spec)
        if not entry.is_file():
            raise FileNotFoundError(f"domain overview entry is missing: {entry}")
        source_path = entry.relative_to(CORE_ROOT).as_posix()
        digest = _sha256(entry)
        inputs.append(
            CanonicalSnapshotInput(
                path=source_path,
                sha256=digest,
                size_bytes=entry.stat().st_size,
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
                    root_key=mount.root_key,
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
            size_bytes=entry.stat().st_size,
            metadata={
                "kind": "domain-overview",
                "domain_slug": spec.slug,
                "strict_mounts": True,
                "source_entry": source_path,
                "theme_version": "research-theme-v1",
            },
            mounts=mounts,
        )
        pointers = {
            field: CanonicalFieldPointer(
                json_pointer=f"/items/{index}/asset/{field}"
            )
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
    except ValueError as exc:
        raise ValueError(f"portal snapshot output must stay below {allowed}") from exc
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
