#!/usr/bin/env python3
"""Download the verified Video recovery queue into a core-domain staging area.

The downloader is intentionally narrow: it accepts only the evidence-map schema
created for the 2026-08-31 Video batch and only official HTTPS hosts.  Every
response must be a parseable PDF before it is atomically promoted from `.part`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

ALLOWED_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "openreview.net",
    "openaccess.thecvf.com",
}
MAX_BYTES = 200 * 1024 * 1024
TITLE_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass
class Result:
    index: int
    title: str
    url: str
    final_url: str | None
    path: str | None
    status: str
    sha256: str | None = None
    size: int | None = None
    pages: int | None = None
    content_type: str | None = None
    title_overlap: float | None = None
    error: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("official_pdf_url must be credential-free HTTPS")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"host is not on the official allowlist: {host or '<missing>'}")


def stable_filename(record: dict[str, Any]) -> str:
    index = int(record["index"])
    identifier = str(record.get("arxiv_id") or "").strip()
    if not identifier:
        landing = str(record.get("official_landing_url") or "")
        identifier = urllib.parse.urlsplit(landing).query or Path(
            urllib.parse.urlsplit(landing).path
        ).name
    identifier = re.sub(r"[^A-Za-z0-9._-]+", "-", identifier).strip("-._")
    if not identifier:
        identifier = hashlib.sha256(str(record["title"]).encode("utf-8")).hexdigest()[:12]
    return f"missing-{index:02d}-{identifier}.pdf"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_words(text: str) -> set[str]:
    return {word.casefold() for word in TITLE_WORD_RE.findall(text) if len(word) > 1}


def inspect_pdf(path: Path, expected_title: str) -> tuple[int, float]:
    with path.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise ValueError("response has no PDF magic in the first 1024 bytes")
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("downloaded PDF is locked")
    pages = len(reader.pages)
    if pages < 1:
        raise ValueError("downloaded PDF has no pages")
    first_text = ""
    for page in reader.pages[:2]:
        try:
            first_text += "\n" + (page.extract_text() or "")
        except Exception:
            continue
    expected = normalized_words(expected_title)
    observed = normalized_words(first_text)
    overlap = len(expected & observed) / len(expected) if expected else 0.0
    return pages, round(overlap, 4)


def validate_existing(path: Path, record: dict[str, Any], url: str) -> Result:
    pages, overlap = inspect_pdf(path, str(record["title"]))
    return Result(
        index=int(record["index"]),
        title=str(record["title"]),
        url=url,
        final_url=url,
        path=str(path),
        status="reused_validated" if overlap >= 0.55 else "reused_needs_title_review",
        sha256=sha256_file(path),
        size=path.stat().st_size,
        pages=pages,
        title_overlap=overlap,
    )


def download_one(record: dict[str, Any], output_dir: Path, retries: int) -> Result:
    index = int(record["index"])
    title = str(record["title"])
    url = str(record["official_pdf_url"])
    destination = output_dir / stable_filename(record)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        validate_url(url)
        if destination.exists():
            return validate_existing(destination, record, url)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "ResearchAssetHub/2026-08-31 paper-recovery",
                        "Accept": "application/pdf",
                    },
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    final_url = response.geturl()
                    validate_url(final_url)
                    content_type = response.headers.get_content_type()
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > MAX_BYTES:
                        raise ValueError(f"response exceeds {MAX_BYTES} bytes")
                    with temporary.open("wb") as handle:
                        copied = 0
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > MAX_BYTES:
                                raise ValueError(f"response exceeds {MAX_BYTES} bytes")
                            handle.write(chunk)
                pages, overlap = inspect_pdf(temporary, title)
                digest = sha256_file(temporary)
                size = temporary.stat().st_size
                os.replace(temporary, destination)
                return Result(
                    index=index,
                    title=title,
                    url=url,
                    final_url=final_url,
                    path=str(destination),
                    status="downloaded" if overlap >= 0.55 else "downloaded_needs_title_review",
                    sha256=digest,
                    size=size,
                    pages=pages,
                    content_type=content_type,
                    title_overlap=overlap,
                )
            except (OSError, ValueError, urllib.error.URLError) as error:
                last_error = error
                temporary.unlink(missing_ok=True)
                if attempt < retries:
                    time.sleep(2**attempt)
        raise RuntimeError(str(last_error or "download failed"))
    except Exception as error:
        temporary.unlink(missing_ok=True)
        return Result(
            index=index,
            title=title,
            url=url,
            final_url=None,
            path=None,
            status="error",
            error=f"{type(error).__name__}: {error}",
        )


def load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != 40:
        raise ValueError("recovery map must contain exactly 40 records")
    indexes = [int(record["index"]) for record in records]
    if sorted(indexes) != list(range(1, 41)):
        raise ValueError("recovery indexes must be exactly 1..40")
    for record in records:
        validate_url(str(record.get("official_pdf_url") or ""))
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    records = load_records(args.map.resolve(strict=True))
    if args.dry_run:
        rows = [
            {
                "index": int(record["index"]),
                "title": record["title"],
                "url": record["official_pdf_url"],
                "destination": str(args.output_dir / stable_filename(record)),
            }
            for record in records
        ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 8))) as pool:
        futures = {
            pool.submit(download_one, record, args.output_dir, max(0, args.retries)): record
            for record in records
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[{result.index:02d}/40] {result.status}: {result.title}", flush=True)

    results.sort(key=lambda item: item.index)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    report = {
        "schema_version": "video-pdf-recovery/v1",
        "generated_at": utc_now(),
        "source_map": str(args.map.resolve()),
        "output_dir": str(args.output_dir.resolve()),
        "counts": counts,
        "records": [asdict(result) for result in results],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = args.report.with_suffix(args.report.suffix + ".tmp")
    temporary_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary_report, args.report)
    errors = sum(1 for result in results if result.status == "error")
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
