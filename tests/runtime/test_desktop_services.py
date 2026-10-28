"""The shared desktop process must expose both independent HTTP applications."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _listening_ports(ports: tuple[int, ...]) -> list[int]:
    listening = []
    for port in ports:
        with socket.socket() as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
    return listening


def _assert_ports_closed(ports: tuple[int, ...], *, timeout: float = 5) -> None:
    # taskkill /T returning and the venv launcher exiting do not imply every
    # descendant's Winsock listener has finished closing on Windows. Observe
    # the actual contract with a bounded deadline, not an arbitrary sleep.
    deadline = time.monotonic() + timeout
    while listening := _listening_ports(ports):
        assert time.monotonic() < deadline, f"HTTP listeners still active after exit: {listening}"
        time.sleep(0.05)


def test_port_shutdown_waits_for_actual_socket_release(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(time, "sleep", lambda _seconds: listener.close())
        _assert_ports_closed((port,))


def test_port_shutdown_still_rejects_a_persistent_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(AssertionError, match="HTTP listeners still active"):
            _assert_ports_closed((listener.getsockname()[1],), timeout=0)


def test_desktop_process_serves_both_origins_and_stops_together(tmp_path: Path) -> None:
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        ports = (first.getsockname()[1], second.getsockname()[1])
    source = tmp_path / "sources.yaml"
    source.write_text("schema_version: 1\nworkspace_root: .\nsources: []\n", encoding="utf-8")
    env = {
        **os.environ,
        "HUB_API_PORT": str(ports[0]),
        "HUB_ASSET_GATEWAY_PORT": str(ports[1]),
        "HUB_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "HUB_DATABASE_REQUIRE_MIGRATIONS": "false",
        "HUB_SOURCES_CONFIG_PATH": str(source),
        "VIRTUAL_ENV": sys.prefix,
    }
    with (tmp_path / "services.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "apps.desktop_services"],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            schemas = []
            deadline = time.monotonic() + 30
            for port in ports:
                while True:
                    assert process.poll() is None, (tmp_path / "services.log").read_text()
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/openapi.json", timeout=1
                        ) as response:
                            schemas.append(json.load(response))
                        break
                    except (urllib.error.URLError, TimeoutError):
                        assert time.monotonic() < deadline, "HTTP services did not become ready"
                        time.sleep(0.05)
            assert "/api/v1/dashboard" in schemas[0]["paths"]
            assert "/api/v1/dashboard" not in schemas[1]["paths"]
            assert "/health" in schemas[1]["paths"]
            assert schemas[0]["info"]["title"] != schemas[1]["info"]["title"]
        finally:
            if os.name == "nt":
                # Windows venv python.exe is a launcher; mirror the desktop
                # supervisor's process-tree shutdown rather than kill only it.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=True,
                )
            else:
                process.terminate()
            process.wait(timeout=10)
    _assert_ports_closed(ports)
