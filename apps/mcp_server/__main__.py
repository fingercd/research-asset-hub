"""Run with python -m apps.mcp_server; stdout is reserved for MCP protocol."""

from __future__ import annotations

import logging
import sys

from apps.mcp_server.server import create_server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        server = create_server()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
