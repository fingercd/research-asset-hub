"""Prepare paper-title QA JSONL from paper_metadata and Video recovery evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

STATUS_SUFFIX_RE = re.compile(r"\s*[（(](?:已读|未读)[）)]\s*$", re.IGNORECASE)
MINERU_PAGE_SUFFIX_RE = re.compile(r"_\d+-\d+$")
NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).replace("\x00", " ").split())


def _path_text(value: Any) -> str:
    """Preserve filesystem spelling; NFKC may name a different Windows file."""

    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _normal_stem(value: str) -> str:
    stem = STATUS_SUFFIX_RE.sub("", Path(value).stem)
    stem = MINERU_PAGE_SUFFIX_RE.sub("", stem)
    return " ".join(NON_WORD_RE.sub(" ", stem.casefold()).split())


def _field_value(container: dict[str, Any], key: str) -> Any:
    candidate = container.get(key)
    if not isinstance(candidate, dict):
        return None
    return candidate.get("value")


def _string_value(value: Any) -> str:
    if isinstance(value, list):
        return _text(", ".join(str(item) for item in value if item))
    return _text(value)


def _metadata_value(record: dict[str, Any], field: str) -> str:
    embedded = record.get("embedded_metadata")
    facts = record.get("facts")
    if not isinstance(embedded, dict):
        embedded = {}
    if not isinstance(facts, dict):
        facts = {}
    if field == "title":
        embedded_keys = ("/Title", "Title", "dc:title")
        fact_keys = ("title",)
    else:
        embedded_keys = ("/Author", "Author", "dc:creator")
        fact_keys = ("authors", "author")
    for key in embedded_keys:
        value = _string_value(embedded.get(key))
        if value:
            return value
    xmp = record.get("xmp_metadata")
    if isinstance(xmp, dict):
        for key in embedded_keys:
            value = _string_value(xmp.get(key))
            if value:
                return value
    for key in fact_keys:
        value = _string_value(_field_value(facts, key))
        if value:
            return value
    return ""


def _read_metadata_latest(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid paper_metadata JSONL line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"paper_metadata line {line_number} is not an object")
            source = record.get("source")
            if not isinstance(source, dict) or not _path_text(source.get("path")):
                raise ValueError(f"paper_metadata line {line_number} has no source.path")
            key = _path_text(source.get("path_key")) or os.path.normcase(
                _path_text(source["path"])
            )
            latest[key] = record
    return list(latest.values())


def _mineru_index(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        raise ValueError(f"Video MinerU root is not a directory: {root}")
    return sorted(
        ((_normal_stem(path.name), path.resolve()) for path in root.rglob("*.md")),
        key=lambda item: str(item[1]).casefold(),
    )


def _stem_score(pdf_stem: str, md_stem: str) -> float:
    if pdf_stem == md_stem:
        return 1.0
    shorter, longer = sorted((pdf_stem, md_stem), key=len)
    if len(shorter) >= 25 and longer.startswith(shorter):
        return min(0.99, 0.88 + 0.11 * len(shorter) / len(longer))
    left = set(pdf_stem.split())
    right = set(md_stem.split())
    overlap = len(left & right) / len(left | right) if left | right else 0.0
    sequence = SequenceMatcher(None, pdf_stem, md_stem).ratio()
    return max(overlap, sequence)


def _match_mineru(pdf_path: Path, index: Sequence[tuple[str, Path]]) -> Path | None:
    target = _normal_stem(pdf_path.name)
    ranked = sorted(
        ((_stem_score(target, stem), path) for stem, path in index),
        key=lambda item: (-item[0], str(item[1]).casefold()),
    )
    if not ranked or ranked[0][0] < 0.86:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.025:
        return None
    return ranked[0][1]


def _is_video_path(path: Path) -> bool:
    return any(part.casefold() == "video" for part in path.parts)


def _record_id(record: dict[str, Any], path: Path) -> str:
    source = record.get("source")
    assert isinstance(source, dict)
    sha256 = _text(source.get("sha256"))
    if re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        return f"desktop-{sha256.casefold()[:24]}"
    digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    return f"desktop-path-{digest[:24]}"


def _metadata_item(record: dict[str, Any], mineru: Sequence[tuple[str, Path]]) -> dict[str, Any]:
    source = record["source"]
    path = Path(_path_text(source["path"]))
    item: dict[str, Any] = {"id": _record_id(record, path), "pdf_path": str(path)}
    title = _metadata_value(record, "title")
    authors = _metadata_value(record, "authors")
    if title:
        item["metadata_title"] = title
    if authors:
        item["metadata_authors"] = authors
    if _is_video_path(path):
        match = _match_mineru(path, mineru)
        if match is not None:
            item["mineru_path"] = str(match)
    return item


def _auto_download_report(map_path: Path) -> Path | None:
    candidates = [
        map_path.with_name(map_path.name.replace("-recovery-map", "-download")),
        map_path.with_name("2026-08-31-video-missing-40-download.json"),
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _recovered_paths(map_path: Path, recovered_pdf_root: Path | None) -> dict[int, Path]:
    if recovered_pdf_root is not None:
        root = recovered_pdf_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"recovered PDF root is not a directory: {root}")
        result: dict[int, Path] = {}
        for index in range(1, 1000):
            matches = sorted(root.glob(f"missing-{index:02d}-*.pdf"))
            if not matches:
                continue
            if len(matches) != 1:
                raise ValueError(f"ambiguous recovered PDFs for index {index}: {matches}")
            result[index] = matches[0].resolve()
        return result
    report_path = _auto_download_report(map_path)
    if report_path is None:
        raise ValueError(
            "recovery map needs --recovered-pdf-root when no adjacent download report exists"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    records = report.get("records") if isinstance(report, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"download report has no records list: {report_path}")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        index = record.get("index")
        raw_path = record.get("path")
        if not isinstance(index, int) or not isinstance(raw_path, str):
            continue
        path = Path(raw_path).resolve(strict=True)
        if path.suffix.casefold() != ".pdf":
            raise ValueError(f"download report index {index} is not a PDF: {path}")
        result[index] = path
    return result


def _recovery_items(
    map_path: Path, video_root: Path, recovered_pdf_root: Path | None
) -> list[dict[str, Any]]:
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    records = mapping.get("records") if isinstance(mapping, dict) else None
    if not isinstance(records, list):
        raise ValueError("Video recovery map has no records list")
    paths = _recovered_paths(map_path, recovered_pdf_root)
    items: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Video recovery map record is not an object")
        index = record.get("index")
        title = _text(record.get("title"))
        markdown = _path_text(record.get("markdown"))
        if not isinstance(index, int) or index <= 0 or not title or not markdown:
            raise ValueError(f"invalid Video recovery record: {record}")
        if index in seen_indexes:
            raise ValueError(f"duplicate Video recovery index: {index}")
        seen_indexes.add(index)
        pdf_path = paths.get(index)
        if pdf_path is None:
            raise ValueError(f"no recovered PDF for Video recovery index {index}")
        mineru_path = (video_root / Path(markdown)).resolve(strict=True)
        if not mineru_path.is_file():
            raise ValueError(f"no MinerU Markdown for Video recovery index {index}: {mineru_path}")
        items.append(
            {
                "id": f"video-recovered-{index:02d}",
                "pdf_path": str(pdf_path),
                "mineru_path": str(mineru_path),
                "reference_title": title,
            }
        )
    return items


def prepare_inputs(
    metadata_jsonl: Path,
    output: Path,
    *,
    video_mineru_root: Path | None = None,
    recovery_map: Path | None = None,
    recovered_pdf_root: Path | None = None,
    expected_count: int = 376,
) -> dict[str, Any]:
    metadata_jsonl = metadata_jsonl.resolve(strict=True)
    output = output.resolve(strict=False)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    if recovery_map is not None and video_mineru_root is None:
        raise ValueError("--recovery-map requires --video-mineru-root")
    mineru: list[tuple[str, Path]] = []
    video_root: Path | None = None
    if video_mineru_root is not None:
        supplied_root = video_mineru_root.resolve(strict=True)
        nested_mineru = supplied_root / "work" / "mineru"
        if nested_mineru.is_dir():
            video_root = supplied_root
            mineru = _mineru_index(nested_mineru)
        elif (
            supplied_root.name.casefold() == "mineru"
            and supplied_root.parent.name.casefold() == "work"
        ):
            video_root = supplied_root.parent.parent
            mineru = _mineru_index(supplied_root)
        else:
            video_root = supplied_root
            mineru = _mineru_index(supplied_root)
    metadata_records = _read_metadata_latest(metadata_jsonl)
    items = [_metadata_item(record, mineru) for record in metadata_records]
    metadata_count = len(items)
    recovery_added = 0
    recovery_enriched = 0
    if recovery_map is not None:
        assert video_root is not None
        recovery_items = _recovery_items(
            recovery_map.resolve(strict=True), video_root, recovered_pdf_root
        )
        item_by_path = {os.path.normcase(str(Path(item["pdf_path"]))): item for item in items}
        for recovery_item in recovery_items:
            key = os.path.normcase(str(Path(recovery_item["pdf_path"])))
            existing = item_by_path.get(key)
            if existing is None:
                items.append(recovery_item)
                item_by_path[key] = recovery_item
                recovery_added += 1
                continue
            existing["mineru_path"] = recovery_item["mineru_path"]
            existing["reference_title"] = recovery_item["reference_title"]
            existing["recovery_map_id"] = recovery_item["id"]
            recovery_enriched += 1
    ids = [str(item["id"]) for item in items]
    if len(ids) != len(set(ids)):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        raise ValueError(f"duplicate QA ids: {duplicates[:10]}")
    paths = [os.path.normcase(str(Path(item["pdf_path"]))) for item in items]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate PDF paths across metadata and recovery inputs")
    if len(items) != expected_count:
        raise ValueError(f"expected {expected_count} QA items, prepared {len(items)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "metadata_latest": metadata_count,
        "recovery_added": recovery_added,
        "recovery_enriched": recovery_enriched,
        "total": len(items),
        "unique_ids": len(set(ids)),
        "video_with_mineru": sum("mineru_path" in item for item in items),
        "output": str(output),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_metadata_jsonl", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--video-mineru-root", type=Path)
    parser.add_argument("--recovery-map", type=Path)
    parser.add_argument("--recovered-pdf-root", type=Path)
    parser.add_argument("--expected-count", type=int, default=376)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.expected_count <= 0:
        raise SystemExit("--expected-count must be positive")
    try:
        summary = prepare_inputs(
            args.paper_metadata_jsonl,
            args.output,
            video_mineru_root=args.video_mineru_root,
            recovery_map=args.recovery_map,
            recovered_pdf_root=args.recovered_pdf_root,
            expected_count=args.expected_count,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
