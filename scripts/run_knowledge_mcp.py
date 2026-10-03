"""Location-independent stdio entrypoint for the installed Codex MCP configuration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    # Initialize/list tools without starting or requiring the platform backend.
    # HubClient wakes the shared desktop supervisor only on an actual tool read.
    from apps.mcp_server.__main__ import main

    main()
