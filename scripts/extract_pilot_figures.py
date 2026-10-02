"""Extract reviewable figure crops for the local paper pilot."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from apps.asset_gateway.registry import SourceRegistry
from packages.knowledge.figures import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    SourceHashMismatch,
    apply_caption_review_warnings,
    extract_pdf_figures,
    sha256_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JAVA = PROJECT_ROOT / "work" / "tools" / "java" / "jdk8u504-b01" / "bin" / "java.exe"
DEFAULT_PDFFIGURES2_JAR = PROJECT_ROOT / "work" / "tools" / "pdffigures2" / "pdffigures2.jar"


class PDFFigures2Error(RuntimeError):
    """Raised when the local PDFFigures2 CLI fails to produce JSON output."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pilot",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "knowledge-pilot" / "pilot.json",
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "sources.yaml",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "knowledge-pilot" / "v1",
    )
    parser.add_argument(
        "--baseline-output-root",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "knowledge-pilot" / "pymupdf-baseline" / "v1",
    )
    parser.add_argument("--extractor", choices=("auto", "pdffigures2", "pymupdf"), default="auto")
    parser.add_argument("--pdffigures2-java", type=Path, default=DEFAULT_JAVA)
    parser.add_argument("--pdffigures2-jar", type=Path, default=DEFAULT_PDFFIGURES2_JAR)
    parser.add_argument("--java-heap", default="768m")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--baseline-count", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--asset-id", action="append", default=[])
    parser.add_argument("--corrections", type=Path)
    parser.add_argument("--mark-reviewed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dpi", type=int, default=220)
    parser.add_argument("--preview-dpi", type=int, default=90)
    return parser.parse_args()


def _pilot_entries(document: Any) -> list[dict[str, Any]]:
    entries: Any
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        entries = document.get("papers", document.get("items", []))
    else:
        raise ValueError("pilot JSON must be a list or an object with papers/items.")
    if not isinstance(entries, list):
        raise ValueError("pilot papers/items must be a JSON array.")
    return [dict(entry) for entry in entries]


def _safe_asset_id(value: Any) -> str:
    return str(UUID(str(value)))


def _corrections_for_entry(
    payload: dict[str, Any] | None, entry: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    asset_id = _safe_asset_id(entry["asset_id"])
    if "papers" in payload:
        paper = payload.get("papers", {}).get(asset_id)
        if paper is None:
            return {}
    else:
        if payload.get("asset_id") and _safe_asset_id(payload["asset_id"]) != asset_id:
            return {}
        paper = payload
    if (
        paper.get("source_hash")
        and str(paper["source_hash"]).casefold() != str(entry["source_hash"]).casefold()
    ):
        raise ValueError(f"Correction source_hash does not match paper {asset_id}.")
    raw = paper.get("figures", {})
    if isinstance(raw, list):
        corrections: dict[str, dict[str, Any]] = {}
        for row in raw:
            figure_id = str(row["figure_id"])
            if figure_id in corrections:
                raise ValueError(f"Duplicate correction for figure {figure_id}.")
            corrections[figure_id] = {
                "reviewer": paper.get("reviewer", payload.get("reviewer")),
                "human_reviewed": paper.get("human_reviewed", payload.get("human_reviewed", False)),
                **dict(row),
            }
        return corrections
    if isinstance(raw, dict):
        return {
            str(figure_id): {
                "reviewer": paper.get("reviewer", payload.get("reviewer")),
                "human_reviewed": paper.get("human_reviewed", payload.get("human_reviewed", False)),
                **dict(row),
            }
            for figure_id, row in raw.items()
        }
    raise ValueError("Correction file figures must be an array or object.")


def _run_pdffigures2(
    pdf_path: Path,
    *,
    java_path: Path,
    jar_path: Path,
    java_heap: str,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="rah-pdffigures2-") as temporary_directory:
        temporary = Path(temporary_directory)
        data_prefix = temporary / "data-"
        stats_path = temporary / "stats.json"
        command = [
            str(java_path),
            f"-Xmx{java_heap}",
            "-Dfile.encoding=UTF-8",
            "-Dsun.java2d.cmm=sun.java2d.cmm.kcms.KcmsServiceProvider",
            "-jar",
            str(jar_path),
            str(pdf_path),
            "-d",
            str(data_prefix),
            "-s",
            str(stats_path),
            "-i",
            "150",
            "-t",
            "1",
            "-q",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise PDFFigures2Error(f"Timed out after {timeout_seconds}s.") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise PDFFigures2Error(f"CLI exited {result.returncode}: {detail}")
        outputs = sorted(path for path in temporary.glob("data-*.json") if path != stats_path)
        if len(outputs) != 1:
            raise PDFFigures2Error(f"Expected one PDFFigures2 JSON output, found {len(outputs)}.")
        rows = json.loads(outputs[0].read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise PDFFigures2Error("PDFFigures2 JSON root was not an array.")
    return [dict(row) for row in rows], {
        "duration_seconds": round(time.perf_counter() - started, 3),
        "cli_item_count": len(rows),
    }


def _cached_manifest(
    manifest_path: Path, source_hash: str, *, force: bool
) -> dict[str, Any] | None:
    if not manifest_path.exists() or force:
        return None
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    if existing.get("source_hash") != source_hash:
        raise FileExistsError(
            "Existing figure manifest belongs to a different PDF hash; pass --force to replace it."
        )
    manifest = cast(dict[str, Any], existing)
    apply_caption_review_warnings(manifest)
    _write_json(manifest_path, manifest)
    return manifest


def _failure(entry: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "asset_id": str(entry.get("asset_id", "")),
        "representation_id": str(entry.get("representation_id", "")),
        "source_id": str(entry.get("source_id", "")),
        "path_ref": str(entry.get("path_ref", "")),
        "title": str(entry.get("title", "")),
        "status": code,
        "error": message,
        "figure_count": 0,
        "table_count": 0,
        "warnings": [],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    arguments = _arguments()
    if not 1 <= arguments.workers <= 4:
        raise SystemExit("--workers must be between 1 and 4.")
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be positive.")
    if arguments.baseline_count < 0:
        raise SystemExit("--baseline-count cannot be negative.")
    if arguments.timeout_seconds < 1:
        raise SystemExit("--timeout-seconds must be positive.")
    if arguments.mark_reviewed and arguments.corrections is None:
        raise SystemExit("--mark-reviewed requires --corrections.")

    pilot_document = json.loads(arguments.pilot.read_text(encoding="utf-8"))
    entries = _pilot_entries(pilot_document)
    if arguments.asset_id:
        requested = {_safe_asset_id(value) for value in arguments.asset_id}
        entries = [entry for entry in entries if _safe_asset_id(entry["asset_id"]) in requested]
    if arguments.limit is not None:
        entries = entries[: arguments.limit]
    if not entries:
        raise SystemExit("No pilot papers matched the selection.")

    corrections_payload = (
        json.loads(arguments.corrections.read_text(encoding="utf-8"))
        if arguments.corrections
        else None
    )
    registry = SourceRegistry.load(arguments.source_config)
    java_path = arguments.pdffigures2_java
    jar_path = arguments.pdffigures2_jar
    use_pdffigures2 = arguments.extractor == "pdffigures2" or (
        arguments.extractor == "auto" and java_path.is_file() and jar_path.is_file()
    )
    if use_pdffigures2 and (not java_path.is_file() or not jar_path.is_file()):
        raise SystemExit(
            "PDFFigures2 mode requires existing --pdffigures2-java and --pdffigures2-jar files."
        )

    valid_jobs: list[dict[str, Any]] = []
    paper_results: list[dict[str, Any]] = []
    for raw_entry in entries:
        entry = dict(raw_entry)
        try:
            entry["asset_id"] = _safe_asset_id(entry["asset_id"])
            pdf_path = registry.resolve(
                str(entry["source_id"]),
                str(entry["root_key"]),
                str(entry["path_ref"]),
            )
            source_hash = str(entry["source_hash"]).casefold()
            if sha256_file(pdf_path) != source_hash:
                raise SourceHashMismatch("Pilot source_hash does not match the PDF bytes.")
            output_directory = arguments.output_root / entry["asset_id"]
            manifest_path = output_directory / "figures.json"
            cached = _cached_manifest(
                manifest_path,
                source_hash,
                force=arguments.force or bool(_corrections_for_entry(corrections_payload, entry)),
            )
            if cached is not None:
                paper_results.append(
                    {
                        "asset_id": entry["asset_id"],
                        "representation_id": entry["representation_id"],
                        "source_id": entry["source_id"],
                        "path_ref": entry["path_ref"],
                        "title": entry.get("title"),
                        "status": "cached",
                        "manifest_path": manifest_path.relative_to(
                            arguments.output_root
                        ).as_posix(),
                        "figure_count": len(cached.get("figures", [])),
                        "table_count": int(cached.get("pdffigures2_tables_excluded", 0)),
                        "warnings": cached.get("warnings", []),
                        "manifest": cached,
                    }
                )
                continue
            valid_jobs.append(
                {
                    "entry": entry,
                    "pdf_path": pdf_path,
                    "source_hash": source_hash,
                    "output_directory": output_directory,
                    "corrections": _corrections_for_entry(corrections_payload, entry),
                }
            )
        except Exception as error:
            paper_results.append(_failure(entry, "failed", f"{type(error).__name__}: {error}"))

    official_results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    if use_pdffigures2:
        with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
            futures = {
                executor.submit(
                    _run_pdffigures2,
                    job["pdf_path"],
                    java_path=java_path,
                    jar_path=jar_path,
                    java_heap=arguments.java_heap,
                    timeout_seconds=arguments.timeout_seconds,
                ): job
                for job in valid_jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    official_results[job["entry"]["asset_id"]] = future.result()
                except Exception as error:
                    paper_results.append(
                        _failure(
                            job["entry"],
                            "failed_pdffigures2",
                            f"{type(error).__name__}: {error}",
                        )
                    )

    successful_jobs: list[dict[str, Any]] = []
    for job in valid_jobs:
        entry = job["entry"]
        result = official_results.get(entry["asset_id"])
        if use_pdffigures2 and result is None:
            continue
        try:
            corrections = job["corrections"]
            if use_pdffigures2:
                assert result is not None
                rows, cli_report = result
                manifest = extract_pdf_figures(
                    job["pdf_path"],
                    job["output_directory"],
                    asset_id=entry["asset_id"],
                    representation_id=str(entry["representation_id"]),
                    source_hash=job["source_hash"],
                    title=str(entry.get("title") or "") or None,
                    dpi=arguments.dpi,
                    preview_dpi=arguments.preview_dpi,
                    corrections=corrections,
                    mark_corrected_reviewed=arguments.mark_reviewed,
                    pdffigures2_results=rows,
                    run_pymupdf_heuristic=False,
                )
            else:
                cli_report = None
                manifest = extract_pdf_figures(
                    job["pdf_path"],
                    job["output_directory"],
                    asset_id=entry["asset_id"],
                    representation_id=str(entry["representation_id"]),
                    source_hash=job["source_hash"],
                    title=str(entry.get("title") or "") or None,
                    dpi=arguments.dpi,
                    preview_dpi=arguments.preview_dpi,
                    corrections=corrections,
                    mark_corrected_reviewed=arguments.mark_reviewed,
                )
            paper_row = {
                "asset_id": entry["asset_id"],
                "representation_id": entry["representation_id"],
                "source_id": entry["source_id"],
                "path_ref": entry["path_ref"],
                "title": entry.get("title"),
                "status": "candidate_extracted",
                "manifest_path": (job["output_directory"] / "figures.json")
                .relative_to(arguments.output_root)
                .as_posix(),
                "figure_count": len(manifest["figures"]),
                "table_count": manifest.get("pdffigures2_tables_excluded", 0),
                "warnings": manifest["warnings"],
                "manifest": manifest,
            }
            if cli_report is not None:
                paper_row["pdffigures2"] = cli_report
            paper_results.append(paper_row)
            successful_jobs.append(job)
        except Exception as error:
            paper_results.append(
                _failure(entry, "failed_extraction", f"{type(error).__name__}: {error}")
            )

    baseline_results: list[dict[str, Any]] = []
    baseline_jobs = successful_jobs[: arguments.baseline_count] if use_pdffigures2 else []
    for job in baseline_jobs:
        entry = job["entry"]
        try:
            manifest = extract_pdf_figures(
                job["pdf_path"],
                arguments.baseline_output_root / entry["asset_id"],
                asset_id=entry["asset_id"],
                representation_id=str(entry["representation_id"]),
                source_hash=job["source_hash"],
                title=str(entry.get("title") or "") or None,
                dpi=arguments.dpi,
                preview_dpi=arguments.preview_dpi,
            )
            baseline_results.append(
                {
                    "asset_id": entry["asset_id"],
                    "representation_id": entry["representation_id"],
                    "title": entry.get("title"),
                    "status": "candidate_extracted",
                    "manifest_path": (
                        arguments.baseline_output_root / entry["asset_id"] / "figures.json"
                    )
                    .relative_to(arguments.baseline_output_root)
                    .as_posix(),
                    "figure_count": len(manifest["figures"]),
                    "warnings": manifest["warnings"],
                }
            )
        except Exception as error:
            baseline_results.append(
                _failure(entry, "failed_pymupdf_baseline", f"{type(error).__name__}: {error}")
            )

    paper_results.sort(key=lambda item: str(item.get("asset_id", "")))
    aggregate_figures: list[dict[str, Any]] = []
    for paper in paper_results:
        aggregate_manifest = paper.get("manifest")
        if not isinstance(aggregate_manifest, dict):
            continue
        for figure in aggregate_manifest.get("figures", []):
            aggregate_figures.append(
                {
                    **figure,
                    "asset_id": paper["asset_id"],
                    "representation_id": paper["representation_id"],
                    "title": paper.get("title"),
                    "source_id": paper.get("source_id"),
                    "source_hash": aggregate_manifest.get("source_hash"),
                    "manifest_path": paper.get("manifest_path"),
                }
            )

    candidate_count = sum(int(paper.get("figure_count", 0)) for paper in paper_results)
    excluded_tables = sum(int(paper.get("table_count", 0)) for paper in paper_results)
    status_counts: dict[str, int] = {}
    for paper in paper_results:
        status = str(paper.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    samefig_manifest = {
        "schema_version": 1,
        "status": "candidate_extraction",
        "pilot_path": str(arguments.pilot.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "extractor": (
            {"name": "pdffigures2", "version": "0.1.0", "workers": arguments.workers}
            if use_pdffigures2
            else {"name": EXTRACTOR_NAME, "version": EXTRACTOR_VERSION}
        ),
        "requested_papers": len(entries),
        "status_counts": status_counts,
        "figure_count": candidate_count,
        "excluded_table_count": excluded_tables,
        "papers": [
            {key: value for key, value in paper.items() if key != "manifest"}
            for paper in paper_results
        ],
        "figures": aggregate_figures,
        "pymupdf_baseline": baseline_results,
        "review_required": True,
    }
    _write_json(arguments.output_root / "samefigmanifest.json", samefig_manifest)
    report = {
        "status": "passed"
        if all(item.get("status") in {"candidate_extracted", "cached"} for item in paper_results)
        else "partial",
        "requested": len(entries),
        "completed": sum(
            item.get("status") in {"candidate_extracted", "cached"} for item in paper_results
        ),
        "status_counts": status_counts,
        "candidate_figures": candidate_count,
        "excluded_tables": excluded_tables,
        "baseline_count": len(baseline_results),
        "errors": [
            item for item in paper_results if str(item.get("status", "")).startswith("failed")
        ],
        "samefigmanifest": str(
            (arguments.output_root / "samefigmanifest.json").relative_to(PROJECT_ROOT)
        ).replace("\\", "/"),
    }
    _write_json(arguments.output_root / "batch-report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
