"""Build a 75-paper VLN release-status sidecar from audited local evidence."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT.parent
VLN_ROOT = CORE_ROOT / "VLN"
SOURCE_ID = "vln-release-status-audit-20260904"
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"
DEFAULT_REPORT = PROJECT_ROOT / "work" / "validation" / SOURCE_ID / "build-report.json"
AUDITED_AT = "2026-09-03"

QUESTIONABLE_CODE_IDS = {
    "mapdream-task-driven-map-learning-vision-language-2026",
    "monodream-monocular-vision-language-navigation-panoramic-dreaming-2025",
}

CHECKPOINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "matterport3d-dataset-90-building-scale-scenes-10-2017": ("not_applicable", ()),
    "speaker-follower-panoramic-action-space-synthetic-r2r-2018": ("open", ()),
    "envdrop-mixed-imitation-reinforcement-learning-environmental-dropout-2019": ("open", ()),
    "prevalent-pre-training-image-text-action-triplets-2020": ("open", ()),
    "recurrent-vln-bert-r2r-reverie-2021": ("open", ()),
    "duet-dual-scale-graph-transformer-online-topological-2022": ("open", ()),
    "bevbert-hybrid-local-metric-map-global-topological-2023": ("open", ()),
    "navillm-schema-based-generalist-navigation-model-cvdn-2024": (
        "open",
        ("https://huggingface.co/zd11024/NaviLLM",),
    ),
    "etp-r1-evolving-topological-planning-reinforcement-fine-2025": ("open", ()),
    "agrivln-vision-language-navigation-agricultural-robots-2025": (
        "not_applicable",
        (),
    ),
    "correctnav-self-correction-flywheel-empowers-vision-language-2025": (
        "open",
        ("https://disk.pku.edu.cn/link/AAFD453AC93DEE4A5F8C84C14CC73D0AC1",),
    ),
    "dreamnav-trajectory-based-imaginative-framework-zero-shot-2025": (
        "not_applicable",
        (),
    ),
    "gc-vln-instruction-as-graph-constraints-training-2025": ("not_applicable", ()),
    "lavira-language-vision-robot-actions-translation-zero-2025": (
        "not_applicable",
        (),
    ),
    "progress-think-semantic-progress-reasoning-vision-language-2025": (
        "open",
        ("https://huggingface.co/HorizonRobotics/Progress-Think",),
    ),
    "ground-slow-move-fast-dual-system-foundation-2025": (
        "open",
        ("https://huggingface.co/InternRobotics/InternVLA-N1-DualVLN",),
    ),
    "vl-ln-bench-towards-long-horizon-goal-2025": (
        "open",
        ("https://huggingface.co/InternRobotics/VL-LN-Bench-basemodel",),
    ),
    "vlnverse-benchmark-vision-language-navigation-versatile-embodied-2025": (
        "not_applicable",
        (),
    ),
    "vln-mme-diagnosing-mllms-as-language-guided-2025": ("not_applicable", ()),
    "fantasyvln-unified-multimodal-chain-thought-reasoning-vision-2026": (
        "open",
        ("https://huggingface.co/acvlab/FantasyVLN",),
    ),
    "sparse-video-generation-propels-real-world-beyond-2026": (
        "open",
        ("https://huggingface.co/OpenDriveLab/SparseVideoNav_VGM",),
    ),
    "openfrontier-general-navigation-visual-language-grounded-frontiers-2026": (
        "not_applicable",
        (),
    ),
    "navtrust-benchmarking-trustworthiness-embodied-navigation-2026": (
        "not_applicable",
        (),
    ),
    "ga-vln-geometry-aware-bev-representation-efficient-2026": (
        "open",
        ("https://huggingface.co/jahhao/gavln_official",),
    ),
    "futurenav-unified-world-action-modeling-vision-language-2026": (
        "open",
        ("https://huggingface.co/llxs/FutureNav",),
    ),
    "abot-n1-toward-general-visual-language-navigation-2026": (
        "not_applicable",
        (),
    ),
    "agentvln-towards-agentic-vision-language-navigation-2026": ("announced", ()),
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def _existing_code_rows() -> dict[str, dict[str, Any]]:
    document = _load(
        PROJECT_ROOT
        / "config"
        / "intake"
        / "existing-code-vision-language-navigation-20260831"
        / "manifest.json"
    )
    return {str(item["title"]): item for item in document["items"]}


def _release_status(
    paper: dict[str, Any],
    repository_status: dict[str, Any],
    existing_code: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    paper_id = str(paper["paper_id"])
    repository_url = paper.get("official_repo") or repository_status.get("url")
    downloaded = repository_status.get("status") == "downloaded"
    code_open = "official_open" if downloaded else "unknown"
    code_local = "full" if downloaded else "absent"
    enrichment = existing_code.get(str(paper["title"]))
    if enrichment is not None:
        code = enrichment.get("representations", {}).get("code", {})
        repository_url = code.get("url") or repository_url
        code_local = "full" if code.get("status") == "official_local" else code_local
        if paper_id not in QUESTIONABLE_CODE_IDS:
            code_open = "official_open"
    if paper_id == "ga-vln-geometry-aware-bev-representation-efficient-2026":
        code_open = "official_open"
        code_local = "full"
        repository_url = "https://github.com/jahhaoyang/GA-VLN"
    if paper_id == "agentvln-towards-agentic-vision-language-navigation-2026":
        code_open = "official_open"
        code_local = "full"
        repository_url = "https://github.com/Allenxinn/AgentVLN"
    if paper_id == "think-like-pilot-fine-grained-long-horizon-2026":
        code_open = "official_announced"
    if paper_id == "vln-nf-feasibility-aware-vision-language-navigation-2026":
        code_open = "official_announced"
    if paper_id == "spatial-vln-zero-shot-vision-language-navigation-2026":
        code_open = "unavailable"
    checkpoint_open, checkpoint_urls = CHECKPOINTS.get(paper_id, ("unknown", ()))
    evidence_urls = list(
        dict.fromkeys(
            value
            for value in [repository_url, *checkpoint_urls, *(paper.get("source_urls") or [])]
            if isinstance(value, str) and value.startswith(("http://", "https://"))
        )
    )
    return {
        "code_open": code_open,
        "code_local": code_local,
        "checkpoint_open": checkpoint_open,
        "repository_url": repository_url,
        "checkpoint_urls": list(checkpoint_urls),
        "verified_at": AUDITED_AT,
        "evidence_urls": evidence_urls,
    }


def build(output: Path, report_path: Path) -> dict[str, Any]:
    paper_document = _load(VLN_ROOT / "library" / "catalog" / "papers.v1.json")
    papers = list(paper_document["papers"])
    repository_document = _load(
        VLN_ROOT / "library" / "catalog" / "repository_download_status.json"
    )
    repositories = {str(item["paper_id"]): item for item in repository_document["items"]}
    existing_code = _existing_code_rows()
    items: list[dict[str, Any]] = []
    for paper in papers:
        paper_id = str(paper["paper_id"])
        items.append(
            {
                "id": f"release-audit-{paper_id}",
                "title": paper["title"],
                "authors": paper.get("authors") or [],
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "doi": paper.get("doi"),
                "arxiv_id": paper.get("arxiv_id"),
                "source_urls": list(
                    dict.fromkeys(
                        value
                        for value in [
                            paper.get("landing_url"),
                            *(paper.get("pdf_urls") or []),
                            paper.get("official_repo"),
                        ]
                        if value
                    )
                ),
                "tags": ["vision-language-navigation", "release-status-audit"],
                "attributes": {
                    "first_public_date": paper.get("first_public_date"),
                    "release_status": _release_status(
                        paper, repositories.get(paper_id, {}), existing_code
                    ),
                },
            }
        )

    intake_sources = (
        ("intake-awarevln-2026", "2026-05-21", "open"),
        ("intake-streamvln-2026", "2025-07-07", "unknown"),
        ("intake-internvla-n1-2026", None, "unknown"),
    )
    for source_id, first_public_date, checkpoint_open in intake_sources:
        source = _load(PROJECT_ROOT / "config" / "intake" / source_id / "manifest.json")
        paper = source["items"][0]
        code = paper.get("representations", {}).get("code", {})
        checkpoint_urls = [
            value
            for value in paper.get("source_urls", [])
            if "huggingface.co" in value and "/datasets/" not in value
        ]
        items.append(
            {
                "id": f"release-audit-{paper['id']}",
                "title": paper["title"],
                "authors": paper.get("authors") or [],
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "doi": paper.get("doi"),
                "arxiv_id": paper.get("arxiv_id"),
                "source_urls": paper.get("source_urls") or [],
                "tags": ["vision-language-navigation", "release-status-audit"],
                "attributes": {
                    "first_public_date": first_public_date,
                    "release_status": {
                        "code_open": (
                            "official_open" if code.get("url") else "unknown"
                        ),
                        "code_local": "absent",
                        "checkpoint_open": checkpoint_open,
                        "repository_url": code.get("url"),
                        "checkpoint_urls": checkpoint_urls,
                        "verified_at": AUDITED_AT,
                        "evidence_urls": paper.get("source_urls") or [],
                    },
                },
            }
        )

    document = {
        "schema_version": "research-asset-hub.generic-manifest/v1",
        "source_id": SOURCE_ID,
        "audited_at": AUDITED_AT,
        "items": items,
    }
    _write(output, document)
    report = {
        "status": "built",
        "papers": len(items),
        "code_open": sum(
            item["attributes"]["release_status"]["code_open"] == "official_open"
            for item in items
        ),
        "code_local": sum(
            item["attributes"]["release_status"]["code_local"] == "full"
            for item in items
        ),
        "checkpoint_open": sum(
            item["attributes"]["release_status"]["checkpoint_open"] == "open"
            for item in items
        ),
        "checkpoint_unknown": sum(
            item["attributes"]["release_status"]["checkpoint_open"] == "unknown"
            for item in items
        ),
        "output": str(output.resolve()),
    }
    _write(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.output, arguments.report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
