"""Exercise the installed stdio entrypoint against the real, read-only local pilot."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from packages.domain.exporters.common import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


async def verify() -> int:
    pilot = json.loads((ROOT / "outputs/knowledge-pilot/pilot.json").read_text(encoding="utf-8"))
    paper = pilot["papers"][1]  # The frozen pilot explicitly selects DDIM here.
    figure = json.loads(
        (ROOT / "outputs/knowledge-pilot/v1" / paper["asset_id"] / "figures.json").read_text(
            encoding="utf-8"
        )
    )["figures"][0]
    code = json.loads(
        (ROOT / "config/knowledge-methodology/code-pilot.json").read_text(encoding="utf-8")
    )["cards"][0]["evidence"][0]
    calls = [
        ("library_status", {}),
        ("search_assets", {"query": "DDIM", "asset_type": "paper", "limit": 3}),
        ("get_paper", {"paper_id": paper["asset_id"]}),
        (
            "read_evidence",
            {
                "paper_id": paper["asset_id"],
                "representation_id": paper["representation_id"],
                "page_start": 1,
                "page_end": 2,
                "max_chars": 2000,
            },
        ),
        ("get_figure", {"figure_id": figure["figure_id"]}),
        (
            "read_code",
            {
                "asset_id": code["asset_id"],
                "representation_id": code["representation_id"],
                "path": code["path"],
                "line_start": code["line_start"],
                "line_end": code["line_end"],
            },
        ),
        ("get_methodology", {"kind": "writing", "domain": "generation", "limit": 3}),
    ]
    environment = dict(os.environ)
    environment["HUB_MCP_API_URL"] = "http://127.0.0.1:8877/api/v1"
    environment["HUB_MCP_POWERSHELL"] = str(
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
    )
    parameters = StdioServerParameters(
        command=str(ROOT / ".venv/Scripts/python.exe"),
        args=[str(ROOT / "scripts/run_knowledge_mcp.py")],
        cwd=str(Path.home()),
        env=environment,
    )
    rows: list[dict[str, Any]] = []
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=180)
        ) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            print(
                "initialized", initialized.serverInfo.name, "tools", len(listed.tools), flush=True
            )
            for name, arguments in calls:
                started = perf_counter()
                result = await session.call_tool(name, arguments)
                body = result.structuredContent or {}
                row = {
                    "tool": name,
                    "is_error": bool(result.isError),
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "content_types": [item.type for item in result.content],
                    "structured_keys": sorted(body),
                }
                if result.isError:
                    row["error"] = [item.text for item in result.content if item.type == "text"]
                elif name == "search_assets":
                    row.update(
                        backend=body.get("backend"),
                        degraded=body.get("degraded"),
                        titles=[x["title"] for x in body.get("items", [])],
                    )
                elif name == "get_paper":
                    row.update(asset_id=body.get("asset_id"), figures=len(body.get("figures", [])))
                elif name == "read_evidence":
                    row.update(
                        source_type=body.get("source_type"),
                        source_hash=body.get("source_hash"),
                        characters=len(body.get("text", "")),
                        has_more=body.get("has_more"),
                    )
                elif name == "get_figure":
                    row["image_bytes"] = sum(
                        len(base64.b64decode(item.data))
                        for item in result.content
                        if item.type == "image"
                    )
                    row["quality_status"] = body.get("quality_status")
                elif name == "get_methodology":
                    row["returned_cards"] = len(body.get("items", []))
                print(json.dumps(row, ensure_ascii=False), flush=True)
                rows.append(row)
    result_document = {
        "generated_at": datetime.now(UTC).isoformat(),
        "transport": "official MCP ClientSession over stdio",
        "launcher_cwd": "outside_project",
        "read_only_tools": all(t.annotations and t.annotations.readOnlyHint for t in listed.tools),
        "tools": rows,
        "passed": len(rows) == 7 and not any(r["is_error"] for r in rows),
    }
    atomic_write_json(
        ROOT / "work/validation/knowledge-pilot-20261002/mcp-live-smoke.json", result_document
    )
    return 0 if result_document["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
