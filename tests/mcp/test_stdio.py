"""Real MCP initialize/list/call against a disposable, GET-only HTTP fixture."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
ASSET = "11111111-1111-4111-8111-111111111111"
REP = "22222222-2222-4222-8222-222222222222"


async def test_real_stdio_initialize_list_and_call() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            mime = "application/json"
            if "/figures/" in self.path and "/image?" in self.path:
                body = b"\x89PNG\r\n\x1a\nfixture"
                mime = "image/png"
            elif self.path.endswith("/representations"):
                body = json.dumps(
                    {
                        "items": [
                            {
                                "id": REP,
                                "representation_type": "repository",
                                "availability": "available",
                                "local_available": True,
                            }
                        ]
                    }
                ).encode()
            elif "/code/file?" in self.path:
                body = json.dumps(
                    {
                        "previewable": True,
                        "content": "source line\n",
                        "line_count": 1,
                    }
                ).encode()
            else:
                body = json.dumps({"status": "test-fixture", "read_only": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    env = {**os.environ, "HUB_MCP_API_URL": f"http://127.0.0.1:{fixture.server_port}/api/v1"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.mcp_server"],
        cwd=str(ROOT),
        env=env,
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.serverInfo.name == "Research Asset Hub"
                tools = await session.list_tools()
                assert len(tools.tools) == 7
                assert all(tool.annotations.readOnlyHint for tool in tools.tools)
                response = await session.call_tool("library_status", {})
                assert not response.isError
                assert response.structuredContent == {"status": "test-fixture", "read_only": True}
                for tool, args in [
                    ("search_assets", {"query": "fixture"}),
                    ("get_paper", {"paper_id": ASSET}),
                    ("read_evidence", {"paper_id": ASSET}),
                    ("get_methodology", {"domain": "image-editing", "kind": "figure"}),
                    ("read_code", {"asset_id": ASSET, "path": "main.py"}),
                ]:
                    called = await session.call_tool(tool, args)
                    assert not called.isError, called
                    assert called.structuredContent is not None
                figure = await session.call_tool("get_figure", {"figure_id": f"{ASSET}-fig-1"})
                assert not figure.isError, figure
                assert figure.structuredContent == {"status": "test-fixture", "read_only": True}
                assert figure.content[1].type == "image"
                assert figure.content[1].mimeType == "image/png"
                invalid = await session.call_tool("search_assets", {"query": "x", "limit": 21})
                assert invalid.isError
        assert len(requests) == 9
        assert requests[0] == "/api/v1/knowledge/status"
    finally:
        fixture.shutdown()
        fixture.server_close()
        worker.join(timeout=2)
