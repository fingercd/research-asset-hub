from __future__ import annotations

import argparse
import json
from pathlib import Path

from .builder import apply_copy_plan, build_intake, write_outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build canonical paper records and a non-overwriting copy plan"
    )
    parser.add_argument("--metadata", required=True, type=Path, help="paper_metadata JSONL/CSV")
    parser.add_argument("--video-recovery-map", required=True, type=Path)
    parser.add_argument("--video-recovery-report", type=Path)
    parser.add_argument("--recovered-root", type=Path)
    parser.add_argument("--desktop-root", required=True, type=Path)
    parser.add_argument("--vlm-catalog", required=True, type=Path)
    parser.add_argument(
        "--identity-decisions",
        type=Path,
        help="explicit same-identity alternate-version decisions; never generated automatically",
    )
    parser.add_argument(
        "--routing-decisions",
        type=Path,
        help="explicit evidence-backed decisions for needs-review records; never generated",
    )
    parser.add_argument(
        "--title-qa-results",
        type=Path,
        help="read-only paper_title_qa JSONL matched by source PDF SHA-256",
    )
    parser.add_argument(
        "--title-decisions",
        type=Path,
        help="manual titles for non-accepted QA record_keys; never generated",
    )
    parser.add_argument(
        "--author-resolution",
        type=Path,
        help="read-only resolved authors JSONL matched by record_key and PDF SHA-256",
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=Path("config/taxonomy.yaml"),
        help="taxonomy whose existing domain slugs routing decisions must use",
    )
    parser.add_argument("--core-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected-count", type=int, default=376)
    parser.add_argument(
        "--allow-nonstandard-counts",
        action="store_true",
        help="disable the fixed 55+40+44+20+17+9+191 batch-shape gate",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "copy copy_new plus explicitly approved alternate versions; "
            "never overwrite and verify SHA-256"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_intake(
        metadata_path=args.metadata.resolve(strict=True),
        recovery_map_path=args.video_recovery_map.resolve(strict=True),
        recovery_report_path=(
            args.video_recovery_report.resolve(strict=True)
            if args.video_recovery_report
            else None
        ),
        recovered_root=args.recovered_root.resolve(strict=True) if args.recovered_root else None,
        desktop_root=args.desktop_root.resolve(strict=True),
        vlm_catalog_path=args.vlm_catalog.resolve(strict=True),
        identity_decisions_path=(
            args.identity_decisions.resolve(strict=True) if args.identity_decisions else None
        ),
        routing_decisions_path=(
            args.routing_decisions.resolve(strict=True) if args.routing_decisions else None
        ),
        taxonomy_path=args.taxonomy.resolve(strict=True),
        title_qa_results_path=(
            args.title_qa_results.resolve(strict=True) if args.title_qa_results else None
        ),
        title_decisions_path=(
            args.title_decisions.resolve(strict=True) if args.title_decisions else None
        ),
        author_resolution_path=(
            args.author_resolution.resolve(strict=True) if args.author_resolution else None
        ),
        core_root=args.core_root.resolve(strict=True),
        expected_count=args.expected_count,
        validate_batch_shape=not args.allow_nonstandard_counts,
    )
    if args.apply:
        result = apply_copy_plan(result, core_root=args.core_root)
    outputs = write_outputs(result, args.output_dir.resolve(strict=False))
    print(
        json.dumps(
            {
                "status": result.report["status"],
                "mode": result.report["mode"],
                "record_count": result.report["record_count"],
                "errors": len(result.report["errors"]),
                "outputs": outputs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
