"""Temporarily favor interactive imports over concurrent background work."""

import os
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def startup_priority() -> Iterator[None]:
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    process = kernel.GetCurrentProcess()
    original = kernel.GetPriorityClass(process)
    changed = original == 0x20 and kernel.SetPriorityClass(process, 0x8000)
    try:
        yield
    finally:
        if changed:
            kernel.SetPriorityClass(process, original)
