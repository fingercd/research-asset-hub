#!/usr/bin/env python3
"""Cloud container supervisor with file-backed secret adaptation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType
from urllib.parse import quote


def _read_secret(variable: str) -> str:
    path_value = os.environ.get(variable)
    if not path_value:
        raise RuntimeError(f"{variable} is required")
    value = Path(path_value).read_text(encoding="utf-8").rstrip("\r\n")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RuntimeError(f"{variable} must reference one non-empty text line")
    return value


def _runtime_environment() -> dict[str, str]:
    environment = dict(os.environ)
    password = _read_secret("HUB_DATABASE_URL_FILE")
    gateway_secret = _read_secret("HUB_GATEWAY_SECRET_FILE")
    meilisearch_key = _read_secret("HUB_MEILISEARCH_API_KEY_FILE")
    encoded_password = quote(password, safe="")
    base_url = f"research_hub:{encoded_password}@postgres:5432/research_asset_hub"
    environment.update(
        {
            "DATABASE_URL": f"postgresql+psycopg://{base_url}",
            "HUB_DATABASE_URL": f"postgresql://{base_url}",
            "HUB_GATEWAY_SECRET": gateway_secret,
            "HUB_MEILISEARCH_API_KEY": meilisearch_key,
        }
    )
    return environment


def main() -> int:
    environment = _runtime_environment()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "/app/migrations/alembic.ini",
            "upgrade",
            "head",
        ],
        check=True,
        cwd="/app",
        env=environment,
    )

    commands = (
        (sys.executable, "-m", "apps.api.main"),
        (sys.executable, "-m", "apps.worker.main"),
        (sys.executable, "-m", "apps.asset_gateway.main"),
        (sys.executable, "-m", "apps.api.web_host"),
    )
    processes = [
        subprocess.Popen(command, cwd="/app", env=environment)  # noqa: S603
        for command in commands
    ]
    stopping = False

    def stop_all(_: int, __: FrameType | None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)
    exit_code = 0
    try:
        while not stopping:
            for process in processes:
                result = process.poll()
                if result is not None:
                    exit_code = result or 1
                    stopping = True
                    break
            time.sleep(0.5)
    finally:
        stop_all(signal.SIGTERM, None)
        deadline = time.monotonic() + 15
        for process in processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
