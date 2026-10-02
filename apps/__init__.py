"""Runnable applications for Research Asset Hub."""

import os
import sys

# A packaged desktop can load immutable dependency bytecode from one archive.
# Development and interpreters with another cache tag keep normal imports.
_startup_archive = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    f"python-startup-{sys.implementation.cache_tag}.zip",
)
if os.path.isfile(_startup_archive):
    sys.path.insert(0, _startup_archive)


def _prime_windows_platform() -> None:
    """Get the OS version without starting CMD and its user AutoRun hooks.

    CPython 3.11's platform.machine() otherwise calls `cmd /c ver` indirectly.
    platform_version is the native Windows version, also used by CPython as its
    fallback. Prime the normal uname cache and restore the private helper so
    other platform APIs keep their standard behavior.
    """
    if sys.platform != "win32":
        return
    import platform

    version = ".".join(str(part) for part in sys.getwindowsversion().platform_version)
    original = platform._syscmd_ver  # type: ignore[attr-defined]
    platform._syscmd_ver = lambda *args, **kwargs: ("Windows", "", version)  # type: ignore[attr-defined]
    try:
        platform.uname()
    finally:
        platform._syscmd_ver = original  # type: ignore[attr-defined]


_prime_windows_platform()
