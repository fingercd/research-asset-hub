"""Desktop HTTP services share imports, while retaining separate origins/apps."""

from __future__ import annotations

import os
import threading

from apps.startup_priority import startup_priority

with startup_priority():
    import uvicorn

    from apps.api.config import get_settings
    from apps.api.main import app as api_app
    from apps.asset_gateway.main import app as gateway_app


def main() -> None:
    settings = get_settings()
    # Both apps read the same immutable root registry. Avoid loading and resolving
    # it again on the homepage's first request.
    api_app.state.source_registry = gateway_app.state.registry
    if os.environ.get("HUB_STARTUP_MEASURE") == "1":
        from apps.startup_metrics import StartupMetrics

        api_app.add_middleware(StartupMetrics, origins=settings.desktop_origins)
    if settings.database_require_migrations:
        from apps.migrate import main as migrate

        # Reuse the database driver and models already loaded by this process.
        # No HTTP listener is opened until the schema has been checked/upgraded.
        migrate(wait_seconds=300)
    api = uvicorn.Server(
        uvicorn.Config(api_app, host=settings.api_host, port=settings.api_port, ws="none")
    )
    gateway = uvicorn.Server(
        uvicorn.Config(
            gateway_app,
            host=settings.asset_gateway_host,
            port=settings.asset_gateway_port,
            ws="none",
        )
    )

    def serve_gateway() -> None:
        try:
            gateway.run()
        finally:
            api.should_exit = True

    thread = threading.Thread(target=serve_gateway, name="asset-gateway", daemon=True)
    thread.start()
    try:
        api.run()
    finally:
        gateway.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
