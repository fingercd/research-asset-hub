"""Resolve paper authors without inventing boundaries in flattened PDF text.

This tool intentionally has a narrow contract: it reads the title/author QA evidence,
the approved paper records, and metadata extraction log; it writes a separate JSONL
overlay.  It never edits intake records or source configuration.  A flattened name
line is represented as ``authors_display`` unless commas make the person boundaries
unambiguous.  This is less convenient than guessing, but it is safe for publication.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "paper-author-resolution/v1"
EXPECTED_COUNT = 376
SPACE_RE = re.compile(r"\s+")
MARKER_RE = re.compile(r"(?:\b\d+(?:\s*,\s*\d+)*\b|[*†‡∗✉✦]+)")
LEADING_MARKERS_RE = re.compile(r"^[\s\d,.*†‡∗✉✦$^{}]+")
TRAILING_MARKERS_RE = re.compile(r"[\s\d,.*†‡∗✉✦$^{}]+$")
AFFILIATION_RE = re.compile(
    r"\b(?:university|institute|laborator(?:y|ies)|department|school|college|academy|"
    r"correspondence|email|e-mail|corporation|company|research(?:\s+center|\s+centre)?|"
    r"faculty|hospital|project\s+lead|corresponding\s+author|oracle\s+ai|"
    r"alibaba\s+group|huawei\s+technologies|"
    r"byte\s*dance|tiktok|nvidia|qualcomm|ant\s+group|google|microsoft|meta|ucl\s+ai\s+centre)\b",
    re.IGNORECASE,
)
NAME_TOKEN_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*$")
INITIAL_RE = re.compile(r"^[A-Z]\.?$")


# These are the only six entries that have no PDF layout candidate in the v2
# review.  Every URL is a primary publisher or arXiv API record and is retained
# in each result as reproducible evidence.
OFFICIAL_AUTHORS: dict[str, dict[str, Any]] = {
    "desktop-0485446136e2c9acb4ddffd0": {
        "authors": ["Pengfei He", "Shaowei Wang", "Tse-Hsun Chen"],
        "url": "https://arxiv.org/abs/2502.14925",
        "source": "arxiv_api",
    },
    "desktop-3ed79a58a23e5fc551e35aed": {
        "authors": [
            "Meng Wei",
            "Chenyang Wan",
            "Jiaqi Peng",
            "Xiqian Yu",
            "Yuqiang Yang",
            "Delin Feng",
            "Wenzhe Cai",
            "Chenming Zhu",
            "Tai Wang",
            "Jiangmiao Pang",
            "Xihui Liu",
        ],
        "url": "https://arxiv.org/abs/2512.08186",
        "source": "arxiv_api",
    },
    "desktop-cbb4df50c34a61cf788bbaaa": {
        "authors": [
            "Haotian Luo",
            "Li Shen",
            "Haiying He",
            "Yibo Wang",
            "Shiwei Liu",
            "Wei Li",
            "Naiqiang Tan",
            "Xiaochun Cao",
            "Dacheng Tao",
        ],
        "url": "https://arxiv.org/abs/2501.12570",
        "source": "arxiv_api",
    },
    "desktop-4905eccaa63c55ac623de1d0": {
        "authors": ["Yang Ji", "Ying Sun"],
        "url": "https://aclanthology.org/2026.acl-long.276/",
        "source": "acl_anthology_bibtex",
    },
    "desktop-6603d0893ec709585b2ce607": {
        "authors": [
            "Yongqi An",
            "Chang Lu",
            "Kuan Zhu",
            "Tao Yu",
            "Chaoyang Zhao",
            "Hong Wu",
            "Ming Tang",
            "Jinqiao Wang",
        ],
        "url": "https://arxiv.org/abs/2605.08840",
        "source": "arxiv_api",
    },
    "desktop-8b2c1e3a7f0f4c9643c5239c": {
        "authors": [
            "Junlin Mu",
            "Hantao Huang",
            "Jihang Zhang",
            "Minghui Yu",
            "Tao Wang",
            "Yidong Li",
        ],
        "url": "https://proceedings.neurips.cc/paper_files/paper/2025/hash/00a0ebcad584c59dbc439c2af8793638-Abstract-Conference.html",
        "source": "neurips_proceedings",
    },
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSONL {path}: {exc}") from exc
    if not rows:
        raise ValueError(f"JSONL is empty: {path}")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"JSONL must contain objects: {path}")
    return rows


def _sha(row: dict[str, Any], *, source: str) -> str:
    if source == "qa":
        value = row.get("source", {}).get("sha256")
    elif source == "record":
        value = row.get("pdf", {}).get("sha256")
    else:
        value = row.get("source", {}).get("sha256")
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"missing SHA-256 in {source} row")
    return value.casefold()


def _unique_by_sha(rows: Iterable[dict[str, Any]], *, source: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        digest = _sha(row, source=source)
        if digest in result:
            raise ValueError(f"duplicate SHA-256 in {source}: {digest}")
        result[digest] = row
    return result


def _clean_display(value: str) -> str:
    """Remove annotation marks and clearly non-author tail text, retaining names verbatim."""
    text = unicodedata.normalize("NFKC", value).replace("\x00", " ")
    text = re.sub(r"\$\\?\{?\^\{?", " ", text)
    text = text.replace("\\dagger", " ").replace("\\ast", " ")
    text = MARKER_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip(" ,;|")
    match = AFFILIATION_RE.search(text)
    if match:
        text = text[: match.start()].rstrip(" ,;|")
    text = LEADING_MARKERS_RE.sub("", text)
    text = TRAILING_MARKERS_RE.sub("", text)
    return SPACE_RE.sub(" ", text).strip(" ,;|")


def _name_part(part: str) -> bool:
    words = [word.strip(".()") for word in part.split() if word.strip(".()")]
    if len(words) < 2 or len(words) > 5:
        return False
    return all(NAME_TOKEN_RE.fullmatch(word) or INITIAL_RE.fullmatch(word) for word in words)


def _split_clear_comma_list(display: str) -> list[str] | None:
    """Only split where PDF layout itself supplies comma person delimiters."""
    if "," not in display or "/" in display:
        return None
    candidate = re.sub(r"\s+and\s+", ", ", display, flags=re.IGNORECASE)
    parts = [_clean_display(part) for part in candidate.split(",")]
    parts = [part for part in parts if part]
    if len(parts) < 2 or not all(_name_part(part) for part in parts):
        return None
    return parts


def _layout_evidence(qa: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    author = qa.get("authors")
    if not isinstance(author, dict):
        raise ValueError(f"missing author QA block: {qa.get('record_key')}")
    candidates = author.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"no layout candidate: {qa.get('record_key')}")
    accepted = author.get("accepted")
    selected = next(
        (
            item
            for item in candidates
            if isinstance(accepted, str) and item.get("value") == accepted
        ),
        candidates[0],
    )
    value = selected.get("value")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty layout candidate: {qa.get('record_key')}")
    evidence = selected.get("evidence") if isinstance(selected.get("evidence"), dict) else {}
    return value, {
        "source": "pypdf_first_page_layout",
        "qa_decision": author.get("decision"),
        "qa_accepted": accepted,
        "candidate_confidence": selected.get("confidence"),
        "raw": value,
        "layout_lines": evidence.get("lines", []),
    }


def resolve_rows(
    qa_rows: list[dict[str, Any]],
    record_rows: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    *,
    expected_count: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    qa_by_sha = _unique_by_sha(qa_rows, source="qa")
    records_by_sha = _unique_by_sha(record_rows, source="record")
    metadata_by_sha = _unique_by_sha(metadata_rows, source="metadata")
    if (
        len(qa_by_sha) != expected_count
        or len(records_by_sha) != expected_count
        or len(metadata_by_sha) != expected_count
    ):
        raise ValueError("each input must have the expected unique SHA-256 count")
    if set(qa_by_sha) != set(records_by_sha) or set(qa_by_sha) != set(metadata_by_sha):
        raise ValueError("QA, records, and metadata SHA-256 sets differ")

    results: list[dict[str, Any]] = []
    for digest, qa in qa_by_sha.items():
        record = records_by_sha[digest]
        key = qa.get("record_key")
        if not isinstance(key, str) or not key:
            raise ValueError(f"missing record_key for {digest}")
        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "paper_id": record.get("paper_id"),
            "record_key": key,
            "pdf_sha256": digest,
            "title": record.get("title") or qa.get("title", {}).get("accepted"),
        }
        official = OFFICIAL_AUTHORS.get(key)
        if official is not None:
            base.update(
                {
                    "authors": official["authors"],
                    "status": "verified_from_official_primary_source",
                    "evidence": {
                        "source": official["source"],
                        "url": official["url"],
                        "reason": "no usable PDF first-page layout author candidate",
                        "raw": None,
                    },
                }
            )
        else:
            raw, evidence = _layout_evidence(qa)
            display = _clean_display(raw)
            if not display:
                raise ValueError(f"cleaned author display empty: {key}")
            authors = _split_clear_comma_list(display)
            base["status"] = "verified_from_pdf_first_page_layout"
            base["evidence"] = evidence
            if authors is None:
                base["authors_display"] = display
            else:
                base["authors"] = authors
        values = base.get("authors") or (
            [base.get("authors_display")] if base.get("authors_display") else []
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"empty author resolution: {key}")
        results.append(base)
    results.sort(key=lambda row: (str(row["paper_id"]), row["record_key"]))
    if (
        len(results) != expected_count
        or len({row["record_key"] for row in results}) != expected_count
    ):
        raise ValueError("resolution output is not uniquely complete")
    counts = Counter(row["status"] for row in results)
    counts["authors_array"] = sum("authors" in row for row in results)
    counts["authors_display"] = sum("authors_display" in row for row in results)
    return results, dict(sorted(counts.items()))


def write_outputs(output: Path, rows: list[dict[str, Any]], counts: dict[str, int]) -> Path:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary = output.with_suffix(".md")
    summary.write_text(
        "# 2026-08-31 作者证据归并\n\n"
        f"- records: {len(rows)}\n"
        f"- unique record keys: {len({row['record_key'] for row in rows})}\n"
        f"- authors arrays: {counts.get('authors_array', 0)}\n"
        f"- authors_display fallbacks: {counts.get('authors_display', 0)}\n"
        f"- PDF first-page layout: {counts.get('verified_from_pdf_first_page_layout', 0)}\n"
        f"- official primary source: {counts.get('verified_from_official_primary_source', 0)}\n"
        "\nAll rows contain exactly one non-empty author representation.  "
        "Flattened PDF text is retained as `authors_display` rather than being "
        "heuristically split into potentially false person names.\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title-qa", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows, counts = resolve_rows(
            _read_jsonl(args.title_qa),
            _read_jsonl(args.records),
            _read_jsonl(args.metadata),
            expected_count=args.expected_count,
        )
        summary = write_outputs(args.output, rows, counts)
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"author resolution failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(args.output),
                "summary": str(summary),
                "records": len(rows),
                "counts": counts,
            },
            ensure_ascii=False,
        )
    )
    return 0
