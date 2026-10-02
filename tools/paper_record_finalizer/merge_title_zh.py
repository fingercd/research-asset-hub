"""Merge five reviewed Chinese-title batches into final-v2 records, fail closed."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tools.paper_record_finalizer.finalize import (
    EXPECTED_COUNT,
    _jsonl,
    _map_unique,
)

SCHEMA_VERSION = "paper-record-final/v3-title-zh"
INPUT_SCHEMA_VERSION = "paper-record-final/v2-local-code-audit"
PLACEHOLDER_RE = re.compile(
    r"^(?:todo|tbd|n/?a|none|null|unknown|待翻译|待补充|待定|未翻译|中文标题待定|翻译待定|[-?？]+)$",
    re.IGNORECASE,
)
HAN_RE = re.compile(r"[\u3400-\u9fff]")


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _batch_title(row: dict[str, Any], *, source: str, line: int) -> tuple[str, str, str]:
    paper_id = _text(row.get("paper_id"))
    english = (
        _text(row.get("title")) or _text(row.get("english_title")) or _text(row.get("title_en"))
    )
    title_zh = _text(row.get("title_zh"))
    if paper_id is None or english is None or title_zh is None:
        raise ValueError(
            f"title-zh batch lacks paper_id, English title, or title_zh: {source}:{line}"
        )
    if PLACEHOLDER_RE.fullmatch(title_zh) or not HAN_RE.search(title_zh):
        raise ValueError(f"title_zh is empty, placeholder, or non-Chinese: {source}:{line}")
    return paper_id, english, title_zh


def read_title_zh_batches(batch_paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    paths = list(batch_paths)
    if len(paths) != 5:
        raise ValueError("exactly five title-zh batch JSONL files are required")
    resolved = [path.expanduser().resolve(strict=True) for path in paths]
    if len(set(resolved)) != 5:
        raise ValueError("title-zh batch paths must be distinct")
    rows: list[dict[str, Any]] = []
    for path in sorted(resolved, key=lambda item: item.name.casefold()):
        for line, row in enumerate(_jsonl(path), start=1):
            paper_id, english, title_zh = _batch_title(row, source=path.name, line=line)
            rows.append(
                {
                    "paper_id": paper_id,
                    "title": english,
                    "title_zh": title_zh,
                    "source_batch": path.name,
                    "source_line": line,
                }
            )
    if len(rows) != EXPECTED_COUNT:
        raise ValueError(f"title-zh rows must total {EXPECTED_COUNT}, got {len(rows)}")
    return _map_unique(rows, "paper_id", "title-zh batches")


def merge_records(
    records: list[dict[str, Any]], title_zh_map: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    record_map = _map_unique(records, "paper_id", "final-v2 records")
    if len(record_map) != EXPECTED_COUNT or set(record_map) != set(title_zh_map):
        missing = sorted(set(record_map) - set(title_zh_map))
        extra = sorted(set(title_zh_map) - set(record_map))
        raise ValueError(
            f"title-zh/final-v2 ID sets differ; missing={missing[:5]}, extra={extra[:5]}"
        )
    merged: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for paper_id in sorted(record_map):
        record = json.loads(json.dumps(record_map[paper_id], ensure_ascii=False))
        if record.get("schema_version") != INPUT_SCHEMA_VERSION:
            raise ValueError(
                f"record {paper_id} is not a final-v2 local-code-audit record: "
                f"{record.get('schema_version')!r}"
            )
        title_row = title_zh_map[paper_id]
        if record.get("title") != title_row["title"]:
            raise ValueError(
                f"English title mismatch for {paper_id}: "
                f"batch={title_row['title']!r}, final-v2={record.get('title')!r}"
            )
        record["title_zh"] = title_row["title_zh"]
        record["title_zh_audit"] = {
            "english_title": title_row["title"],
            "match": "exact",
            "source_batch": title_row["source_batch"],
            "source_line": title_row["source_line"],
        }
        record["schema_version"] = SCHEMA_VERSION
        finalization = dict(record.get("finalization") or {})
        finalization["title_zh_batch"] = title_row["source_batch"]
        finalization["title_zh_batch_line"] = title_row["source_line"]
        record["finalization"] = finalization
        merged.append(record)
        audit.append(
            {
                "paper_id": paper_id,
                "english_title": title_row["title"],
                "title_zh": title_row["title_zh"],
                "match": "exact",
                "source_batch": title_row["source_batch"],
                "source_line": title_row["source_line"],
            }
        )
    return merged, audit


def _csv_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        code = record.get("code") or {}
        rows.append(
            {
                "paper_id": record["paper_id"],
                "title": record["title"],
                "title_zh": record["title_zh"],
                "authors": "; ".join(record["authors"])
                if isinstance(record["authors"], list)
                else record["authors"],
                "year": record["year"],
                "venue": record["venue"],
                "code_status": code.get("status"),
                "local_path": code.get("local_path"),
            }
        )
    return rows


def write_outputs(
    *,
    prefix: Path,
    records: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    replace_output: bool,
) -> dict[str, Path]:
    outputs = {
        "json": prefix.with_suffix(".json"),
        "jsonl": prefix.with_suffix(".jsonl"),
        "csv": prefix.with_suffix(".csv"),
        "markdown": prefix.with_suffix(".md"),
        "translation_audit": prefix.with_name(prefix.name + "-title-zh-audit").with_suffix(".json"),
    }
    existing = [path for path in outputs.values() if path.exists()]
    if existing and not replace_output:
        raise FileExistsError(
            "refusing to overwrite title-zh output: " + ", ".join(map(str, existing))
        )
    prefix.parent.mkdir(parents=True, exist_ok=True)
    code_counts = Counter(str((record.get("code") or {}).get("status")) for record in records)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(records),
        "paper_ids": [record["paper_id"] for record in records],
        "code_status_counts": dict(sorted(code_counts.items())),
        "translation_audit_count": len(audit),
        "records": records,
    }
    outputs["json"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    outputs["jsonl"].write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
        ),
        encoding="utf-8",
    )
    csv_rows = _csv_rows(records)
    with outputs["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    outputs["translation_audit"].write_text(
        json.dumps(
            {
                "schema_version": "paper-title-zh-audit/v1",
                "record_count": len(audit),
                "exact_english_title_matches": len(audit),
                "translations": audit,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    unique_id_count = len({record["paper_id"] for record in records})
    outputs["markdown"].write_text(
        "# 2026-08-31 最终 paper-record 中文题名合并\n\n"
        f"- final records: {len(records)} (unique IDs: {unique_id_count})\n"
        f"- exact English-title matches: {len(audit)}\n"
        "- empty or placeholder Chinese titles: 0\n"
        "- local official code records retained: "
        f"{code_counts.get('official_local', 0)}\n"
        f"- translation audit: `{outputs['translation_audit'].name}`\n",
        encoding="utf-8",
    )
    return outputs


def merge_title_zh(
    *,
    records_path: Path,
    batch_paths: list[Path],
    output_prefix: Path,
    replace_output: bool = False,
) -> tuple[dict[str, Path], dict[str, Any]]:
    records = _jsonl(records_path)
    merged, audit = merge_records(records, read_title_zh_batches(batch_paths))
    outputs = write_outputs(
        prefix=output_prefix, records=merged, audit=audit, replace_output=replace_output
    )
    return outputs, {"records": len(merged), "translation_audit": len(audit)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True, help="final-v2 JSONL")
    parser.add_argument("--batch", type=Path, action="append", required=True, help="title-zh JSONL")
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--replace-output", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        outputs, summary = merge_title_zh(
            records_path=args.records,
            batch_paths=args.batch,
            output_prefix=args.output_prefix,
            replace_output=args.replace_output,
        )
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"title-zh merge failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"outputs": {key: str(value) for key, value in outputs.items()}, **summary},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
