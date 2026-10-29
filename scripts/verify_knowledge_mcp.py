"""Exercise the installed stdio entrypoint against the real, read-only local pilot."""

from __future__ import annotations

import argparse
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


async def verify(report_path: Path, *, include_math: bool = False) -> int:
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
    if include_math:
        math_card = json.loads(
            (ROOT / "config/knowledge-methodology/math-pilot.json").read_text(encoding="utf-8")
        )["cards"][0]
        evidence = math_card["evidence"][0]
        calls.extend(
            [
                ("library_status", {"domain": "形式化数学"}),
                (
                    "search_assets",
                    {
                        "query": "LeanDojo的形式化证明方法",
                        "domain": "AI for Math",
                        "keywords": ["LeanDojo"],
                        "constraints": ["提供原文证据"],
                        "limit": 5,
                    },
                ),
                ("get_paper", {"paper_id": evidence["asset_id"]}),
                (
                    "read_evidence",
                    {
                        "paper_id": evidence["asset_id"],
                        "representation_id": evidence["representation_id"],
                        "page_start": evidence["page"],
                        "page_end": evidence["page"],
                        "max_chars": 2000,
                    },
                ),
                ("get_methodology", {"kind": "writing", "domain": "形式化数学", "limit": 10}),
            ]
        )
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
                row: dict[str, Any] = {
                    "tool": name,
                    "arguments": arguments,
                    "is_error": bool(result.isError),
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "content_types": [item.type for item in result.content],
                    "structured_keys": sorted(body),
                    "validation_passed": False,
                }
                if result.isError:
                    row["error"] = [item.text for item in result.content if item.type == "text"]
                elif name == "search_assets":
                    row.update(
                        backend=body.get("backend"),
                        degraded=body.get("degraded"),
                        titles=[x["title"] for x in body.get("items", [])],
                        query_plan=body.get("query_plan"),
                    )
                    row["validation_passed"] = bool(body.get("items")) and not body.get("degraded")
                elif name == "get_paper":
                    row.update(asset_id=body.get("asset_id"), figures=len(body.get("figures", [])))
                    row["validation_passed"] = body.get("asset_id") == arguments["paper_id"]
                elif name == "read_evidence":
                    row.update(
                        source_type=body.get("source_type"),
                        source_hash=body.get("source_hash"),
                        characters=len(body.get("text", "")),
                        has_more=body.get("has_more"),
                    )
                    row["validation_passed"] = (
                        bool(body.get("segments")) and body.get("source_type") == "original"
                    )
                elif name == "get_figure":
                    row["image_bytes"] = sum(
                        len(base64.b64decode(item.data))
                        for item in result.content
                        if item.type == "image"
                    )
                    row["quality_status"] = body.get("quality_status")
                    row["validation_passed"] = row["image_bytes"] > 0
                elif name == "get_methodology":
                    row["returned_cards"] = len(body.get("items", []))
                    row["validation_passed"] = row["returned_cards"] > 0
                elif name == "read_code":
                    row["validation_passed"] = bool(body.get("content"))
                elif name == "library_status":
                    row["schema_version"] = body.get("schema_version")
                    row["counts"] = body.get("counts")
                    row["validation_passed"] = (
                        body.get("schema_version") == 2 and body.get("read_only") is True
                    )
                print(json.dumps(row, ensure_ascii=False), flush=True)
                rows.append(row)
    result_document = {
        "generated_at": datetime.now(UTC).isoformat(),
        "transport": "official MCP ClientSession over stdio",
        "launcher_cwd": "outside_project",
        "read_only_tools": all(t.annotations and t.annotations.readOnlyHint for t in listed.tools),
        "tools": rows,
        "discovered_tool_names": sorted(t.name for t in listed.tools),
        "passed": len(listed.tools) == 7
        and len(rows) == len(calls)
        and all(not r["is_error"] and r["validation_passed"] for r in rows),
    }
    atomic_write_json(report_path, result_document)
    return 0 if result_document["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-math", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "work/validation/knowledge-goal-20261002/mcp-live-smoke.json",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(verify(args.report, include_math=args.include_math)))
