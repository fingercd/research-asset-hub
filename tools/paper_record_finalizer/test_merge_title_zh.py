from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.paper_record_finalizer.merge_title_zh import (
    INPUT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    _batch_title,
    merge_records,
    read_title_zh_batches,
)


def _records() -> list[dict[str, object]]:
    return [
        {
            "paper_id": f"paper-{index:03d}",
            "schema_version": INPUT_SCHEMA_VERSION,
            "title": f"English title {index}",
            "code": {
                "status": "official_local",
                "local_path": f"code/repository-{index}",
                "evidence": [{"source": "local audit"}],
            },
            "pdf_representations": [{"role": "canonical_current"}],
        }
        for index in range(376)
    ]


def _batches(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for batch_number, start in enumerate((0, 76, 151, 226, 301), start=1):
        stop = (76, 151, 226, 301, 376)[batch_number - 1]
        path = tmp_path / f"batch-{batch_number}.jsonl"
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "paper_id": f"paper-{index:03d}",
                        "title": f"English title {index}",
                        "title_zh": f"中文题名 {index}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for index in range(start, stop)
            ),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def test_merge_title_zh_exactly_matches_and_retains_v2_data(tmp_path: Path) -> None:
    records = _records()
    batches = read_title_zh_batches(_batches(tmp_path))
    merged, audit = merge_records(records, batches)
    assert len(merged) == len(audit) == 376
    assert merged[0]["schema_version"] == SCHEMA_VERSION
    assert merged[0]["title_zh"] == "中文题名 0"
    assert merged[0]["code"] == records[0]["code"]
    assert merged[0]["pdf_representations"] == records[0]["pdf_representations"]
    assert audit[0]["match"] == "exact"


def test_merge_title_zh_rejects_english_title_mismatch(tmp_path: Path) -> None:
    records = _records()
    batches = read_title_zh_batches(_batches(tmp_path))
    batches["paper-000"]["title"] = "A different English title"
    with pytest.raises(ValueError, match="English title mismatch"):
        merge_records(records, batches)


def test_title_zh_rejects_obvious_placeholder() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        _batch_title(
            {"paper_id": "paper-001", "title": "English title", "title_zh": "待翻译"},
            source="batch.jsonl",
            line=1,
        )
