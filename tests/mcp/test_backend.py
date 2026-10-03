"""Startup coordination uses disposable state and fakes; never starts the real Hub."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apps.mcp_server import backend
from apps.mcp_server.backend import BackendState, SharedBackend, startup_lock
from apps.mcp_server.client import DEFAULT_API_URL, HubClient


def test_default_client_is_inert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    launch = Mock(side_effect=AssertionError("initialization must not start Hub"))
    probe = Mock(side_effect=AssertionError("initialization must not contact API"))
    monkeypatch.setattr(SharedBackend, "_launch", launch)
    monkeypatch.setattr(SharedBackend, "_probe", probe)
    monkeypatch.delenv("HUB_MCP_API_URL", raising=False)
    client = HubClient()
    assert client.base_url == DEFAULT_API_URL + "/"
    assert client.backend is not None
    assert not tmp_path.exists() or not list(tmp_path.iterdir())
    launch.assert_not_called()
    probe.assert_not_called()


def test_ready_backend_is_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    instance = SharedBackend()
    monkeypatch.setattr(instance, "_probe", lambda: BackendState.READY)
    launch = Mock()
    monkeypatch.setattr(instance, "_launch", launch)
    instance.ensure_ready()
    launch.assert_not_called()


def test_concurrent_chats_launch_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    ready = threading.Event()
    launches: list[int] = []

    def launch() -> None:
        launches.append(1)
        time.sleep(0.05)
        ready.set()

    instances = [SharedBackend(), SharedBackend(), SharedBackend()]
    monkeypatch.setattr(backend, "desktop_is_running", lambda: False)
    for instance in instances:
        monkeypatch.setattr(instance, "_launch", launch)
        monkeypatch.setattr(
            instance,
            "_probe",
            lambda: BackendState.READY if ready.is_set() else BackendState.ABSENT,
        )
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda instance: instance.ensure_ready(), instances))
    assert launches == [1]


def test_separate_chat_processes_launch_once(tmp_path: Path) -> None:
    script = """
import os
import time
from pathlib import Path
from apps.mcp_server import backend
root = Path(os.environ["HUB_DATA_DIR"])
backend.desktop_is_running = lambda: False
def probe(self):
    return backend.BackendState.READY if (root / "ready").exists() else backend.BackendState.ABSENT
def launch(self):
    with (root / "launches").open("ab") as stream:
        stream.write(b"1")
    time.sleep(0.1)
    (root / "ready").touch()
backend.SharedBackend._probe = probe
backend.SharedBackend._launch = launch
backend.SharedBackend().ensure_ready()
"""
    environment = {**os.environ, "HUB_DATA_DIR": str(tmp_path)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=backend.PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    try:
        for process in processes:
            _, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert (tmp_path / "launches").read_bytes() == b"1"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8877/api/v1", "http://localhost:8767/api/v1"])
async def test_custom_url_never_launches_desktop(url: str) -> None:
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private endpoint")

    client = HubClient(url)
    assert client.backend is None
    client.transport = httpx.MockTransport(disconnected)
    with pytest.raises(ToolError, match="configured local Hub API") as error:
        await client.get_json("knowledge/status")
    assert "private endpoint" not in str(error.value)


def test_existing_desktop_startup_is_waited_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    instance = SharedBackend()
    probe = Mock(side_effect=[BackendState.ABSENT, BackendState.READY])
    launch = Mock()
    monkeypatch.setattr(instance, "_probe", probe)
    monkeypatch.setattr(instance, "_launch", launch)
    monkeypatch.setattr(backend, "desktop_is_running", lambda: True)
    instance.ensure_ready()
    launch.assert_not_called()


def test_running_api_with_missing_dependencies_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    instance = SharedBackend()
    monkeypatch.setattr(
        instance, "_probe", Mock(side_effect=[BackendState.STARTING, BackendState.READY])
    )
    launch = Mock()
    monkeypatch.setattr(instance, "_launch", launch)
    instance.ensure_ready()
    launch.assert_not_called()


def test_failed_startup_is_explicit_and_leaves_owner_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(backend, "STARTUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(backend, "desktop_is_running", lambda: True)
    instance = SharedBackend()
    monkeypatch.setattr(instance, "_probe", lambda: BackendState.ABSENT)
    launch = Mock()
    monkeypatch.setattr(instance, "_launch", launch)
    with pytest.raises(ToolError, match="did not become ready"):
        instance.ensure_ready()
    launch.assert_not_called()


def test_startup_file_lock_is_released_after_failure(tmp_path: Path) -> None:
    path = tmp_path / "startup.lock"
    with pytest.raises(RuntimeError):
        with startup_lock(path, time.monotonic() + 1):
            raise RuntimeError("fixture")
    with startup_lock(path, time.monotonic() + 1):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop supervisor")
def test_launcher_has_no_terminal_and_uses_existing_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "desktop.exe"
    executable.write_bytes(b"fake exe, never executed")
    monkeypatch.setenv("HUB_MCP_DESKTOP_EXE", str(executable))
    import pythoncom
    import win32com.client

    initialize, uninitialize = Mock(), Mock()
    shell = Mock()
    monkeypatch.setattr(pythoncom, "CoInitialize", initialize)
    monkeypatch.setattr(pythoncom, "CoUninitialize", uninitialize)
    dispatch = Mock(return_value=shell)
    monkeypatch.setattr(win32com.client, "Dispatch", dispatch)
    SharedBackend()._launch()
    dispatch.assert_called_once_with("Shell.Application")
    shell.Windows().FindWindowSW.assert_called_once_with(0, 0, 8, 0, 1)
    shell.Windows().FindWindowSW().Document.Application.ShellExecute.assert_called_once_with(
        str(executable), "--background", str(executable.parent), "open", 0
    )
    initialize.assert_called_once()
    uninitialize.assert_called_once()


@pytest.mark.parametrize("status", [302, 404, 500])
def test_probe_failure_never_exposes_response_body(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    original = backend.httpx.Client
    monkeypatch.setattr(
        backend.httpx,
        "Client",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, text="secret value")
            ),
        ),
    )
    with pytest.raises(ToolError, match=f"HTTP {status}") as error:
        SharedBackend()._probe()
    assert "secret value" not in str(error.value)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (httpx.ConnectError, BackendState.ABSENT),
        (httpx.ConnectTimeout, BackendState.ABSENT),
        (httpx.ReadTimeout, BackendState.STARTING),
    ],
)
def test_probe_distinguishes_failed_connections_from_slow_responses(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[httpx.TransportError],
    expected: BackendState,
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise failure("private connection details")

    original = backend.httpx.Client
    monkeypatch.setattr(
        backend.httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(unavailable)),
    )
    assert SharedBackend()._probe() is expected


@pytest.mark.parametrize("connect_failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_first_connect_failure_wakes_once_and_retries_read(
    connect_failure: type[httpx.TransportError],
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if len(requests) == 1:
            raise connect_failure("private error details")
        return httpx.Response(200, json={"read_only": True})

    client = HubClient(transport=httpx.MockTransport(handler))
    owner = SharedBackend()
    owner.ensure_ready = Mock()  # type: ignore[method-assign]
    client.backend = owner
    assert (await client.get_json("knowledge/status"))["read_only"] is True
    assert (await client.get_json("knowledge/status"))["read_only"] is True
    owner.ensure_ready.assert_called_once()
    assert requests == ["/api/v1/knowledge/status"] * 3


async def test_backend_failure_does_not_loop_or_block_the_event_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret value")

    client = HubClient(transport=httpx.MockTransport(handler))
    owner = SharedBackend()
    owner.ensure_ready = Mock(side_effect=ToolError("Shared backend not ready"))  # type: ignore[method-assign]
    client.backend = owner
    with pytest.raises(ToolError, match="Shared backend not ready"):
        await asyncio.wait_for(client.get_json("knowledge/status"), timeout=1)
    owner.ensure_ready.assert_called_once()


async def test_read_retry_failure_is_sanitized_and_does_not_launch_again() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret connection details")

    client = HubClient(transport=httpx.MockTransport(handler))
    owner = SharedBackend()
    owner.ensure_ready = Mock()  # type: ignore[method-assign]
    client.backend = owner
    with pytest.raises(ToolError, match="became unavailable after startup") as error:
        await client.get_json("knowledge/status")
    assert "secret connection details" not in str(error.value)
    owner.ensure_ready.assert_called_once()
