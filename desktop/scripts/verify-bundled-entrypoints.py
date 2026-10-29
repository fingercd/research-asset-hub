"""Import actual desktop launchers with the bundled interpreter and sources."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    stage = args.stage.resolve()
    app = stage / "app"
    runtime = (args.runtime_root or stage / "runtime").resolve()
    assert Path(sys.executable).resolve() == (runtime / "python/python.exe").resolve()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(app))
    with tempfile.TemporaryDirectory(prefix="hub-bundle-import-") as temporary:
        os.environ.update(
            HUB_DATA_DIR=temporary,
            HUB_DATABASE_URL="sqlite+pysqlite:///:memory:",
            HUB_DATABASE_REQUIRE_MIGRATIONS="false",
            HUB_SOURCES_CONFIG_PATH=str(app / "config/sources.yaml"),
            HUB_TAXONOMY_CONFIG_PATH=str(app / "config/taxonomy.yaml"),
            HUB_DOMAIN_PORTAL_CONFIG_PATH=str(app / "config/domain-portals/v2/portal-index.yaml"),
        )
        for name in (
            "apps.desktop_services",
            "apps.worker_start",
            "apps.migrate",
            "apps.startup_metrics",
            "apps.mcp_server.server",
        ):
            importlib.import_module(name)
        loaded = {
            name: str(Path(module.__file__).resolve().relative_to(app))
            for name, module in sys.modules.items()
            if name.split(".")[0] in {"apps", "packages"}
            and getattr(module, "__file__", None)
        }
        from packages.version import __version__

        print(
            json.dumps(
                {
                    "bundled_entrypoints_imported": True,
                    "version": __version__,
                    "bundled_modules": len(loaded),
                }
            )
        )


if __name__ == "__main__":
    main()
