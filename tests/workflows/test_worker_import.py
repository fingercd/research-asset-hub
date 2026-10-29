"""Worker imports must not rely on the API router having loaded first."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_export_package_imports_in_a_fresh_worker_process(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from packages.workflows.packages import export_package; "
            "from apps.worker.handlers import RuntimeHandlers",
        ],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(root)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
