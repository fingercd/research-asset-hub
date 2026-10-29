from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evidence import (
    DEFAULT_MAX_EVIDENCE_CHARS,
    DEFAULT_MAX_EXCERPT_CHARS,
    DEFAULT_MAX_ITEMS_PER_CATEGORY,
    EvidenceError,
    load_paper_records,
    run_batch,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "离线提取带 PDF 页码和章节定位的原文证据 JSON；不生成论文总结，不联网。"
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="单个 paper-record、数组或 papers/items/records 包装 JSON。",
    )
    parser.add_argument("--output", type=Path, required=True, help="证据 JSON 输出目录。")
    parser.add_argument(
        "--base-dir",
        type=Path,
        help="相对 PDF/MinerU 路径的基准目录；默认是 --input 所在目录。",
    )
    parser.add_argument("--checkpoint", type=Path, help="可选断点 JSON 路径。")
    parser.add_argument("--report", type=Path, help="可选批处理报告 JSON 路径。")
    parser.add_argument("--pdf", type=Path, help="单记录时覆盖 paper-record 中的 PDF 路径。")
    parser.add_argument(
        "--mineru-markdown",
        type=Path,
        help="单记录时覆盖 paper-record 中的可选 MinerU Markdown 路径。",
    )
    parser.add_argument(
        "--max-evidence-chars",
        type=int,
        default=DEFAULT_MAX_EVIDENCE_CHARS,
        help=f"每篇所有 excerpt 字符总上限，默认 {DEFAULT_MAX_EVIDENCE_CHARS}。",
    )
    parser.add_argument(
        "--max-excerpt-chars",
        type=int,
        default=DEFAULT_MAX_EXCERPT_CHARS,
        help=f"单条 excerpt 字符上限，默认 {DEFAULT_MAX_EXCERPT_CHARS}。",
    )
    parser.add_argument(
        "--max-items-per-category",
        type=int,
        default=DEFAULT_MAX_ITEMS_PER_CATEGORY,
        help=f"每类证据最多条数，默认 {DEFAULT_MAX_ITEMS_PER_CATEGORY}。",
    )
    parser.add_argument("--force", action="store_true", help="忽略成功断点并重新抽取。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        input_path = args.input.resolve(strict=True)
        records = load_paper_records(input_path)
        base_dir = args.base_dir.resolve(strict=True) if args.base_dir else input_path.parent
        report = run_batch(
            records,
            base_dir=base_dir,
            output_dir=args.output,
            checkpoint_path=args.checkpoint,
            report_path=args.report,
            max_evidence_chars=args.max_evidence_chars,
            max_excerpt_chars=args.max_excerpt_chars,
            max_items_per_category=args.max_items_per_category,
            force=args.force,
            pdf_override=args.pdf,
            mineru_override=args.mineru_markdown,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failed"] == 0 else 1
    except (OSError, EvidenceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
