"""Build the pre-cutoff 2026 VLN backfill found by the multi-agent audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_vln_2026_update import Candidate, build  # noqa: E402

CORE_ROOT = PROJECT_ROOT.parent
SOURCE_ID = "vln-2026-backfill-20260904"
ASSET_ROOT = CORE_ROOT / "个人新增资料" / "2026-09-04-vln-backfill"
CODE_ROOT = CORE_ROOT / "共享代码" / "2026-09-04-vln-backfill" / "upstream"
MANIFEST = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"
WORK_DIR = PROJECT_ROOT / "work" / "validation" / SOURCE_ID

BACKFILL = (
    Candidate("2607.16806", "dynamic-social"),
    Candidate(
        "2606.03682",
        "benchmark-world-model",
        "official_open",
        "https://github.com/TeleHuman/GN0",
        "c92778118542bf1dd7d437855d06e0d822e8361d",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/TeleEmbodied/GN-BAE",),
        project_url="https://telehuman-gn0.github.io/",
    ),
    Candidate("2606.22424", "robust-continuous", venue="ECCV 2026 (author statement)"),
    Candidate(
        "2606.00095",
        "indoor-mapping",
        "official_open",
        "https://github.com/Teacher-Tom/HSGM_public",
        "e2df6e70df869af57d77a32ba658fba04e6814eb",
        checkpoint_open="not_applicable",
        venue="CVPR 2026",
    ),
    Candidate("2605.17249", "indoor-continuous"),
    Candidate(
        "2605.13328",
        "real-robot",
        "official_open",
        "https://github.com/Yunheng-Wang/StereoNav",
        "7d8819df648eefe132519f8a1de7b0370c2ae277",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/anonymous-stereonav/StereoNav_Model",),
        project_url="https://yunheng-wang.github.io/stereonav-public.github.io/",
    ),
    Candidate(
        "2605.13321",
        "dynamic-social",
        "official_announced",
        "https://github.com/HaoxuanXU1024/HCSG",
        project_url="https://haoxuanxu1024.github.io/HCSG/",
        note="Repository remains a coming-soon placeholder.",
    ),
    Candidate("2603.03739", "streaming-real-robot"),
    Candidate(
        "2603.26859",
        "knowledge-augmented",
        "official_open",
        "https://github.com/yds3/IPM_BTK",
        "6ff359dee2e81cc723b99a1476916d82f4c499a6",
        checkpoint_open="open",
        note=(
            "The official repository links processed annotations, features, "
            "and pretrained models."
        ),
    ),
    Candidate(
        "2602.23937",
        "knowledge-augmented",
        "official_announced",
        project_url="https://sites.google.com/view/y-event-kg/",
        note=(
            "The paper/project page announces code and data, but no official "
            "implementation repository was verified."
        ),
    ),
    Candidate("2607.18042", "world-model"),
    Candidate(
        "2606.18112",
        "agentic-generalist",
        "official_announced",
        "https://github.com/QwenLM/Qwen-RobotNav",
        note=(
            "The repository contains the report and demos, not implementation code; "
            "weights are not planned for release."
        ),
    ),
    Candidate(
        "2603.26741",
        "world-model",
        "official_open",
        "https://github.com/F1y1113/LCVN",
        "e7a5f8ce02fbde0eeb8f947a1be4b49c17c9d21d",
        checkpoint_open="not_found",
        checkpoint_urls=("https://huggingface.co/datasets/fly1113/LCVN",),
    ),
    Candidate("2606.06147", "aerial-world-model"),
    Candidate(
        "2607.09792",
        "survey-real-world-evaluation",
        checkpoint_open="not_applicable",
        venue="IEEE TASE 2026",
    ),
    Candidate(
        "2604.25161",
        "diagnostic-benchmark",
        "official_open",
        "https://github.com/JMChen121/CanTest",
        "e6646bf7fa19ea706f2c8786d77b7eb5c9535492",
        checkpoint_open="not_applicable",
        venue="ACL 2026",
    ),
    Candidate("2605.10376", "diagnostic-benchmark", checkpoint_open="not_applicable"),
    Candidate("2605.28237", "outdoor-benchmark", checkpoint_open="not_applicable"),
    Candidate(
        "2606.02519",
        "agricultural-benchmark",
        "official_open",
        "https://github.com/AlexTraveling/IMAC-AgriVLN",
        "ad7aa212c076ea8e81d2d9601aea27411d3b237d",
        checkpoint_open="not_applicable",
    ),
    Candidate("2606.03175", "interactive-dialog", checkpoint_open="not_applicable"),
    Candidate(
        "2606.19948",
        "interactive-dialog",
        "official_open",
        "https://github.com/happilee12/RAINbow",
        "1624af86e77306bad7cd030d5f8f177ecdc7e8cc",
        checkpoint_open="open",
        checkpoint_urls=("https://drive.google.com/file/d/1J51IWSej8PdLUd9VG-H2_knxw1dv_lLY/view",),
        project_url="https://happilee12.github.io/RAINbow",
    ),
    Candidate("2607.03792", "grounding-benchmark", checkpoint_open="not_applicable"),
    Candidate(
        "2607.11063",
        "safety-adversarial",
        checkpoint_open="not_applicable",
        venue="ACM Multimedia 2026",
    ),
    Candidate(
        "2607.26914",
        "biomedical-benchmark",
        "official_open",
        "https://github.com/ActiveButterflies/BioVLN",
        "752e8b5da35fb3dc0157f92c5bf6a83eedb23891",
        checkpoint_open="not_applicable",
    ),
    Candidate("2601.01872", "outdoor"),
    Candidate(
        "2602.09657",
        "aerial",
        "official_announced",
        "https://github.com/xiaolousun/AutoFly-VLA",
        checkpoint_open="announced",
        project_url="https://xiaolousun.github.io/AutoFly/",
        note="Training, validation, data, and checkpoints remain marked Preparing.",
        venue="ICLR 2026",
    ),
    Candidate("2603.07181", "aerial"),
    Candidate("2603.08007", "aerial"),
    Candidate(
        "2603.10682",
        "aerial",
        "official_announced",
        "https://github.com/Robotics-STAR-Lab/OnFly",
        note="Repository says code coming soon.",
    ),
    Candidate(
        "2603.25741",
        "autonomous-driving",
        "official_open",
        "https://github.com/zuosc19/Vega",
        "ecdd8c68747976a3a285b1024e2c06e23d04a421",
        checkpoint_open="announced",
    ),
    Candidate(
        "2604.16298",
        "aerial",
        "official_open",
        "https://github.com/SmartDianLab/FineCogNav",
        "9d2a6db04dd4262622d5697b63bb4a81922a498d",
        checkpoint_open="not_applicable",
        checkpoint_urls=("https://huggingface.co/datasets/Lozumi/AerialVLN-Fine",),
        venue="CVPR 2026 Findings",
    ),
    Candidate(
        "2605.15964",
        "aerial-world-model",
        "official_open",
        "https://github.com/EmbodiedCity/WorldVLN.code",
        "3409b82a16c6606b270b48d7ea4c8f7d08e43c38",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/EmbodiedCity/WorldVLN",),
        project_url="https://embodiedcity.github.io/WorldVLN/",
    ),
    Candidate("2605.31121", "outdoor"),
    Candidate("2606.01205", "aerial-world-model"),
    Candidate(
        "2606.20045",
        "aerial-terminal",
        "official_open",
        "https://github.com/xuefanfu/3DG-VLN",
        "bc83bbb0c9bbccd65872ba7f869e3dac4971c71e",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/fanfuxue/3DG-VLN",),
    ),
    Candidate("2606.28397", "aerial"),
    Candidate("2606.31654", "aerial"),
    Candidate("2607.03146", "aerial"),
    Candidate("2607.08359", "aerial"),
    Candidate("2607.11638", "outdoor-city"),
    Candidate("2607.14997", "aerial-world-model"),
    Candidate("2607.19288", "aerial", checkpoint_open="not_applicable"),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-pdfs", action="store_true")
    arguments = parser.parse_args()
    payload = argparse.Namespace(
        asset_root=ASSET_ROOT,
        code_root=CODE_ROOT,
        manifest=MANIFEST,
        work_dir=WORK_DIR,
        download_pdfs=arguments.download_pdfs,
    )
    print(
        json.dumps(
            build(payload, candidates=BACKFILL, source_id=SOURCE_ID),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
