import os

import pytest

from apps.startup_priority import startup_priority


@pytest.mark.skipif(os.name != "nt", reason="Windows process priority")
def test_bootstrap_priority_is_restored_after_failure() -> None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    process = kernel.GetCurrentProcess()
    original = kernel.GetPriorityClass(process)
    with pytest.raises(RuntimeError, match="import failed"):
        with startup_priority():
            raise RuntimeError("import failed")
    assert kernel.GetPriorityClass(process) == original
