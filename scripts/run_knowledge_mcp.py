"""Location-independent stdio entrypoint for the installed Codex MCP configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    # Codex may start this entrypoint from another workspace. Bootstrap only our
    # documented loopback API; the existing Hub database/search services stay owned
    # by the desktop app. Keep every bootstrap message away from protocol stdout.
    startup = Path(__file__).with_name("start_knowledge_api.ps1")
    result = subprocess.run(
        [
            os.environ.get("HUB_MCP_POWERSHELL", "pwsh"),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(startup),
        ],
        stdin=subprocess.DEVNULL,
        stdout=sys.stderr,
        stderr=sys.stderr,
        timeout=150,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    if result.returncode:
        raise SystemExit(result.returncode)
    from apps.mcp_server.__main__ import main

    main()
