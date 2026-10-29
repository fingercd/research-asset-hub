#!/usr/bin/env python3
"""Recover allowlisted OpenReview PDFs through the user's connected browser.

OpenReview rejects this machine's non-browser HTTP egress with 403.  This helper
uses Kimi WebBridge only for records already present in the verified recovery
map, reads the authenticated browser response in bounded chunks, and writes the
original PDF bytes to the same core-domain staging directory as the regular
downloader.  No cookies, headers, or page storage are exported.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from download_missing_video import inspect_pdf, sha256_file, stable_filename

DAEMON_URL = "http://127.0.0.1:10086/command"
SESSION = "paper-recovery-openreview"
CHUNK_BYTES = 256 * 1024
MAX_BYTES = 200 * 1024 * 1024
OPENREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def command(action: str, args: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(
        {"action": action, "args": args, "session": SESSION},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        DAEMON_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"WebBridge command failed: {payload}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("WebBridge returned malformed data")
    return data


def evaluate(code: str) -> Any:
    data = command("evaluate", {"code": code})
    if data.get("type") == "undefined":
        return None
    return data.get("value")


def record_openreview_id(record: dict[str, Any]) -> str:
    url = str(record.get("official_pdf_url") or "")
    match = re.fullmatch(r"https://openreview\.net/pdf\?id=([A-Za-z0-9_-]+)", url)
    if not match or not OPENREVIEW_ID_RE.fullmatch(match.group(1)):
        raise ValueError(f"record {record.get('index')} is not an allowlisted OpenReview PDF")
    return match.group(1)


def load_selected(path: Path, indexes: set[int]) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("map records are malformed")
    selected = [record for record in records if int(record["index"]) in indexes]
    if {int(record["index"]) for record in selected} != indexes:
        raise ValueError("one or more requested indexes are absent from the recovery map")
    for record in selected:
        record_openreview_id(record)
    return sorted(selected, key=lambda item: int(item["index"]))


def fetch_record(record: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    paper_id = record_openreview_id(record)
    destination = output_dir / stable_filename(record)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if destination.exists():
        pages, overlap = inspect_pdf(destination, str(record["title"]))
        return {
            "index": int(record["index"]),
            "status": "reused_validated",
            "path": str(destination),
            "sha256": sha256_file(destination),
            "size": destination.stat().st_size,
            "pages": pages,
            "title_overlap": overlap,
        }

    init_code = (
        "(async()=>{"
        f"const r=await fetch('/pdf?id={paper_id}',{{credentials:'include'}});"
        "const b=new Uint8Array(await r.arrayBuffer());"
        "window.__codexPdfRecovery=b;"
        "return JSON.stringify({ok:r.ok,status:r.status,type:r.headers.get('content-type'),"
        "size:b.byteLength,magic:Array.from(b.slice(0,8))})"
        "})()"
    )
    metadata = json.loads(str(evaluate(init_code)))
    if not metadata.get("ok") or metadata.get("status") != 200:
        raise RuntimeError(f"OpenReview browser fetch failed: {metadata}")
    size = int(metadata.get("size") or 0)
    if size < 8 or size > MAX_BYTES or metadata.get("magic")[:5] != [37, 80, 68, 70, 45]:
        raise RuntimeError(f"OpenReview returned invalid PDF metadata: {metadata}")

    try:
        with temporary.open("wb") as handle:
            for offset in range(0, size, CHUNK_BYTES):
                length = min(CHUNK_BYTES, size - offset)
                code = (
                    "(()=>{const a=window.__codexPdfRecovery;"
                    f"const b=a.slice({offset},{offset + length});"
                    "let s='';for(let i=0;i<b.length;i+=32768){"
                    "s+=String.fromCharCode(...b.subarray(i,i+32768));}return btoa(s)})()"
                )
                encoded = evaluate(code)
                chunk = base64.b64decode(str(encoded), validate=True)
                if len(chunk) != length:
                    raise RuntimeError(
                        f"chunk length mismatch at {offset}: {len(chunk)} != {length}"
                    )
                handle.write(chunk)
        if temporary.stat().st_size != size:
            raise RuntimeError("assembled file size does not match browser response")
        pages, overlap = inspect_pdf(temporary, str(record["title"]))
        digest = sha256_file(temporary)
        os.replace(temporary, destination)
        return {
            "index": int(record["index"]),
            "status": "downloaded_via_browser",
            "path": str(destination),
            "sha256": digest,
            "size": size,
            "pages": pages,
            "title_overlap": overlap,
        }
    finally:
        temporary.unlink(missing_ok=True)
        try:
            evaluate("(()=>{delete window.__codexPdfRecovery;return true})()")
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--indexes", nargs="+", type=int, required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    records = load_selected(args.map.resolve(strict=True), set(args.indexes))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    first_id = record_openreview_id(records[0])
    command(
        "navigate",
        {
            "url": f"https://openreview.net/forum?id={first_id}",
            "newTab": True,
            "group_title": "论文恢复",
        },
    )
    results: list[dict[str, Any]] = []
    try:
        for record in records:
            result = fetch_record(record, args.output_dir)
            results.append(result)
            print(f"[{result['index']:02d}] {result['status']}", flush=True)
    finally:
        try:
            command("close_session", {})
        except Exception:
            pass

    report = {
        "schema_version": "openreview-browser-recovery/v1",
        "source_map": str(args.map.resolve()),
        "records": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
