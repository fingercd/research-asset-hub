from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import DEFAULT_SOURCES_CONFIG, load_sources, select_sources
from .inspection import (
    build_inventory_report,
    build_preflight_report,
    write_report,
)


def _emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"status: {report.get('status', 'unknown')}")
    if "generated_at" in report:
        print(f"generated_at: {report['generated_at']}")
    if "totals" in report:
        for key, value in report["totals"].items():
            print(f"{key}: {value}")
    if report.get("blocking_errors"):
        print("blocking_errors:")
        for error in report["blocking_errors"]:
            print(f"  - {error}")
    if report.get("errors"):
        print("errors:")
        for error in report["errors"]:
            print(f"  - {error}")


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-asset-hub")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_SOURCES_CONFIG,
        help="Path to sources.yaml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("preflight", "inventory", "validate", "status"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--json", action="store_true")
        if name in {"inventory", "validate"}:
            subparser.add_argument("--all", action="store_true")
            subparser.add_argument("--source")

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--all", action="store_true")
    ingest.add_argument("--source")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--json", action="store_true")
    return parser


def _run_preflight(args: argparse.Namespace) -> int:
    report = build_preflight_report(load_sources(args.config))
    path = write_report(report, "preflight.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _run_inventory(args: argparse.Namespace) -> int:
    sources = select_sources(load_sources(args.config), getattr(args, "source", None), args.all)
    report = build_inventory_report(sources)
    path = write_report(report, "baseline-inventory.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _run_status(args: argparse.Namespace) -> int:
    report_path = Path(__file__).resolve().parents[2] / "work" / "validation"
    report = {
        "status": "bootstrap",
        "message": "Core data services are not required for dry-run ingestion.",
        "reports": {
            "preflight": str(report_path / "preflight.json"),
            "inventory": str(report_path / "baseline-inventory.json"),
            "ingest": str(report_path / "ingest-dry-run.json"),
        },
    }
    _emit(report, args.json)
    return 0


def _run_validate(args: argparse.Namespace) -> int:
    sources = select_sources(load_sources(args.config), args.source, args.all)
    preflight = build_preflight_report(sources)
    inventory = build_inventory_report(sources)
    report = {
        "schema_version": 1,
        "status": (
            "passed"
            if preflight["status"] != "failed" and inventory["status"] != "failed"
            else "failed"
        ),
        "preflight": preflight,
        "inventory": inventory,
    }
    path = write_report(report, "phase-a-validation.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def _run_ingest(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "Refusing non-dry-run ingestion in the first safety checkpoint. Run with --dry-run.",
            file=sys.stderr,
        )
        return 2
    try:
        from packages.ingestion.dry_run import run_dry_run
    except ImportError as exc:
        print(f"Dry-run ingestion is not ready: {exc}", file=sys.stderr)
        return 2

    sources = select_sources(load_sources(args.config), args.source, args.all)
    report = run_dry_run(sources)
    path = write_report(report, "ingest-dry-run.json")
    report["report_path"] = str(path)
    _emit(report, args.json)
    return 1 if report["status"] == "failed" else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _base_parser()
    args = parser.parse_args(argv)
    handlers = {
        "preflight": _run_preflight,
        "inventory": _run_inventory,
        "validate": _run_validate,
        "status": _run_status,
        "ingest": _run_ingest,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
