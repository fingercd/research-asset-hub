"""Wake the existing desktop supervisor on demand, never a second API/worker chain."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum, auto
from pathlib import Path
from typing import BinaryIO

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from packages.managed_sources.service import data_directory, data_directory_fingerprint

DEFAULT_API_URL = "http://127.0.0.1:8767/api/v1"
STARTUP_TIMEOUT_SECONDS = 90.0
DESKTOP_MUTEX = "ResearchAssetHub-SingleInstance"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BackendState(Enum):
    ABSENT = auto()
    STARTING = auto()
    READY = auto()


def desktop_is_running() -> bool:
    """Check the supervisor's existing mutex without acquiring or showing its UI."""
    if os.name != "nt":
        return False
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenMutexW(0x00100000, False, DESKTOP_MUTEX)  # SYNCHRONIZE
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == 5:  # Access denied still proves that an owner exists.
        return True
    if error == 2:  # ERROR_FILE_NOT_FOUND
        return False
    raise ToolError(
        "Cannot verify the shared Hub desktop owner; existing processes were preserved."
    )


def _try_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def startup_lock(path: Path, deadline: float) -> Iterator[None]:
    """OS-owned file lock is shared by chats and released automatically on process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            try:
                _try_lock(stream)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ToolError(
                        "Another chat is restoring Hub; startup wait exceeded "
                        f"{STARTUP_TIMEOUT_SECONDS:g} seconds."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(stream)


class SharedBackend:
    """Inert until ensure_ready; the desktop app remains owner of all child processes."""

    def __init__(self, base_url: str = DEFAULT_API_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._local_lock = threading.Lock()

    def _probe(self) -> BackendState:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(1.0), follow_redirects=False, trust_env=False
            ) as client:
                with client.stream("GET", f"{self.base_url}/knowledge/ready") as response:
                    if response.status_code == 503:
                        return BackendState.STARTING
                    if response.status_code != 200:
                        raise ToolError(
                            "The local API is running but its knowledge readiness route is "
                            f"unavailable (HTTP {response.status_code}); deploy the compatible "
                            "shared API before retrying."
                        )
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 16384:
                            raise ToolError("Shared Hub readiness response exceeds 16 KiB.")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Windows can time out a refused loopback connection before Winsock
            # reports connection-refused. No TCP connection was established.
            return BackendState.ABSENT
        except httpx.TimeoutException:
            return BackendState.STARTING
        except httpx.HTTPError as error:
            raise ToolError(
                "Cannot verify shared Hub readiness; inspect its local service logs."
            ) from error
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError) as error:
            raise ToolError("Shared Hub returned invalid readiness JSON.") from error
        if not isinstance(result, dict) or not (
            result.get("service") == "research-asset-hub-knowledge"
            and result.get("status") == "ready"
            and result.get("read_only") is True
        ):
            raise ToolError(
                "The local API does not identify itself as the compatible Hub knowledge API."
            )
        if (
            result.get("data_directory_sha256") is not None
            and result["data_directory_sha256"] != data_directory_fingerprint(data_directory())
        ):
            raise ToolError(
                "The shared Hub is using a different library directory; select the same library "
                "in the desktop app and refresh the MCP configuration."
            )
        return BackendState.READY

    def _launch(self) -> None:
        if os.name != "nt":
            raise ToolError(
                "Automatic Hub recovery uses the Windows desktop app; start this local API."
            )
        hint = os.getenv("HUB_MCP_DESKTOP_EXE")
        executable = (
            Path(hint)
            if hint
            else Path(os.getenv("ProgramFiles", "C:/Program Files"))
            / "ResearchAssetHub"
            / "research-asset-hub-desktop.exe"
        )
        if not executable.is_absolute() or not executable.is_file() or executable.suffix != ".exe":
            raise ToolError(
                "The configured Hub desktop executable is unavailable; set HUB_MCP_DESKTOP_EXE "
                "to the installed desktop executable."
            )
        # A stdio client may own a Windows kill-on-close Job Object. Ask the
        # existing Explorer desktop to launch Hub so closing one chat cannot
        # terminate the shared application, database and other chats' reads.
        import pythoncom  # type: ignore[import-untyped]
        from win32com.client import Dispatch  # type: ignore[import-untyped]

        try:
            pythoncom.CoInitialize()
            try:
                windows = Dispatch("Shell.Application").Windows()
                desktop = windows.FindWindowSW(0, 0, 8, 0, 1)  # SWC_DESKTOP, NEEDDISPATCH
                desktop.Document.Application.ShellExecute(
                    str(executable),
                    subprocess.list2cmdline(["--background", "--data-dir", str(data_directory())]),
                    str(executable.parent), "open", 0
                )
            finally:
                pythoncom.CoUninitialize()
        except pythoncom.com_error as error:
            raise ToolError(
                "Windows Explorer could not start the shared Hub desktop; open the installed "
                "application and inspect its logs."
            ) from error

    def ensure_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        with self._local_lock:
            data_root = data_directory()
            try:
                with startup_lock(data_root / "state/mcp-backend.lock", deadline):
                    state = self._probe()
                    if state is BackendState.READY:
                        return
                    if state is BackendState.ABSENT and not desktop_is_running():
                        self._launch()
                    while time.monotonic() < deadline:
                        time.sleep(0.1)
                        if self._probe() is BackendState.READY:
                            return
            except OSError as error:
                raise ToolError(
                    "Cannot coordinate shared Hub startup in its local data directory."
                ) from error
        raise ToolError(
            f"Shared Hub did not become ready within {STARTUP_TIMEOUT_SECONDS:g} seconds. "
            "Its desktop owner and existing "
            "processes were preserved; inspect the Hub data directory's logs and retry."
        )
