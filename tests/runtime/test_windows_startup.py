import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version probe")
def test_platform_probe_uses_native_version_without_cmd() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import platform, subprocess, sys; "
            "platform._uname_cache = None; "
            "original = platform._syscmd_ver; "
            "subprocess.check_output = lambda *a, **kw: "
            "(_ for _ in ()).throw(AssertionError('CMD must not be started')); "
            "import apps; "
            "assert platform.machine(); "
            "assert platform.system() == 'Windows'; "
            "native = '.'.join(map(str, sys.getwindowsversion().platform_version)); "
            "assert platform.uname().version == native; "
            "assert platform._syscmd_ver is original",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
