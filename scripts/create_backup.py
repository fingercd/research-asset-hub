"""Create a local platform backup and write a privacy-safe JSON report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packages.backup import create_platform_backup
from packages.domain.exporters.common import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = Path("work") / "validation" / "backup-final.json"
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|credential|api[_-]?key)$", re.I
)
_SENSITIVE_VALUE = re.compile(r"(?:postgres(?:ql)?://|restic_password|bearer\s+)", re.I)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a local Research Asset Hub backup.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Project root (default: repository root).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="JSON report path (default: work/validation/backup-final.json).",
    )
    return parser


def _privacy_safe(value: Any, *, key: str = "") -> Any:
    """Recursively redact credential-like fields and connection strings."""

    if _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _privacy_safe(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_privacy_safe(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        return "<redacted>"
    return value


def _report_path(project_root: Path, requested: Path) -> Path:
    path = requested.expanduser()
    return (project_root / path if not path.is_absolute() else path).resolve()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    project_root = arguments.project_root.expanduser().resolve()
    report_path = _report_path(project_root, arguments.report)
    try:
        result = create_platform_backup(project_root)
        atomic_write_json(report_path, _privacy_safe(result))
    except Exception:
        print("backup failed (see process logs for remediation)", file=sys.stderr)
        return 1
    print(json.dumps(_privacy_safe(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
