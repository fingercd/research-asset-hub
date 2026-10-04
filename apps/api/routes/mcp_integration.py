"""Installation-aware MCP configuration and actual local backend checks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import select

from apps.api import routes_v1 as api
from apps.api.config import PROJECT_ROOT
from packages.domain.models import Asset
from packages.version import __version__

TOOLS = (
    "search_assets",
    "get_paper",
    "read_evidence",
    "read_code",
    "get_figure",
    "get_methodology",
    "library_status",
)


@api.router.get("/integrations/mcp-config")
def mcp_configuration(request: Request) -> dict[str, Any]:
    settings = api._settings(request)
    application_root = PROJECT_ROOT
    installed_python = application_root.parent / "runtime" / "python" / "python.exe"
    python = installed_python if installed_python.is_file() else Path(sys.executable)
    command: dict[str, Any] = {
        "command": str(python),
        "args": ["-m", "apps.mcp_server"],
        "env": {
            "PYTHONPATH": str(application_root),
            "HUB_DATA_DIR": str(settings.data_dir),
            "HUB_MCP_API_URL": f"http://127.0.0.1:{settings.api_port}/api/v1",
        },
    }
    desktop = application_root.parent / "research-asset-hub-desktop.exe"
    if desktop.is_file():
        command["env"]["HUB_MCP_DESKTOP_EXE"] = str(desktop)
    return {
        "version": __version__,
        "server_name": "research-assets",
        "config": {"mcpServers": {"research-assets": command}},
        "command": command,
        "tools": list(TOOLS),
        "read_only": True,
        "data_dir": str(settings.data_dir),
        "bundled_runtime": installed_python.is_file(),
        "module_available": (application_root / "apps" / "mcp_server" / "__main__.py").is_file(),
    }


@api.router.post("/integrations/mcp-check")
def check_mcp_backend(request: Request) -> dict[str, Any]:
    from apps.mcp_server.server import create_server

    configuration = mcp_configuration(request)
    # Construct the same bridge registered by the client. Construction never wakes
    # a second backend; this request is already handled by the shared local API.
    create_server()
    with api._database(request).session() as session:
        paper = session.scalar(
            select(Asset)
            .where(Asset.deleted_at.is_(None), Asset.asset_type == "paper")
            .order_by(Asset.id)
            .limit(1)
        )
        sample = None if paper is None else {"asset_id": str(paper.id), "title": paper.title}
    return {
        "status": "ready",
        "tools": list(TOOLS),
        "tool_count": len(TOOLS),
        "read_only": True,
        "database": "ready",
        "sample": sample,
        "module_available": configuration["module_available"],
        "bundled_runtime": configuration["bundled_runtime"],
        "data_dir": configuration["data_dir"],
        "verification": "bridge_construction_and_database_read",
        "note": "本机模块与数据库读取通过；完整协议与原文读取由客户端连接后验证。",
    }
