"""Safely remove stale, interrupted paper-code clone staging directories."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.paper_code_audit.audit import _is_reparse_point

CONFIRMATION = "CLEAN_STALE_CODE_STAGING"
STAGING_PREFIX = ".paper-code-audit-"


@dataclass(frozen=True, slots=True)
class ProcessReference:
    pid: int
    name: str
    command_line: str


def _windows_processes() -> list[ProcessReference]:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        raise RuntimeError("PowerShell is unavailable for the running-process safety check")
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(git|git-lfs|git-remote-.*|python|pythonw)(\\.exe)?$' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("failed to inspect Windows process command lines")
    if not completed.stdout.strip():
        return []
    payload = json.loads(completed.stdout)
    rows = payload if isinstance(payload, list) else [payload]
    return [
        ProcessReference(
            pid=int(row.get("ProcessId") or 0),
            name=str(row.get("Name") or ""),
            command_line=str(row.get("CommandLine") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _posix_processes() -> list[ProcessReference]:
    result = []
    proc = Path("/proc")
    if not proc.is_dir():
        raise RuntimeError("/proc is unavailable for the running-process safety check")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
            )
        except OSError:
            continue
        if not command:
            continue
        name = Path(command.split()[0]).name
        folded = name.casefold()
        if not (folded.startswith("git") or folded.startswith("python")):
            continue
        result.append(ProcessReference(int(entry.name), name, command))
    return result


def running_git_python_processes() -> list[ProcessReference]:
    return _windows_processes() if os.name == "nt" else _posix_processes()


def _tree_metrics(path: Path) -> tuple[int, int, float]:
    total_size = 0
    file_count = 0
    newest_mtime = path.stat().st_mtime
    for current, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in [*dirnames, *filenames]:
            child = current_path / name
            if _is_reparse_point(child):
                raise ValueError(f"staging tree contains a reparse point: {child}")
            info = child.stat()
            newest_mtime = max(newest_mtime, info.st_mtime)
            if child.is_file():
                total_size += info.st_size
                file_count += 1
    return total_size, file_count, newest_mtime


def _referencing_processes(
    candidate: Path, processes: Sequence[ProcessReference]
) -> list[dict[str, Any]]:
    absolute = str(candidate.resolve()).casefold()
    name = candidate.name.casefold()
    return [
        {"pid": process.pid, "name": process.name}
        for process in processes
        if absolute in process.command_line.casefold() or name in process.command_line.casefold()
    ]


def _validate_direct_staging_child(candidate: Path, archive_root: Path) -> Path:
    if not candidate.name.startswith(STAGING_PREFIX):
        raise ValueError(f"not a paper-code staging directory: {candidate}")
    if _is_reparse_point(candidate):
        raise ValueError(f"staging candidate is a reparse point: {candidate}")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != archive_root:
        raise ValueError(f"staging candidate is not a direct archive-root child: {candidate}")
    if not resolved.is_dir():
        raise ValueError(f"staging candidate is not a directory: {candidate}")
    return resolved


def _remove_readonly(function: Callable[..., Any], path: str, _: Any) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def cleanup_staging(
    *,
    archive_root: Path,
    min_age_seconds: int,
    apply: bool,
    confirmation: str | None,
    now: float | None = None,
    process_provider: Callable[[], Sequence[ProcessReference]] = running_git_python_processes,
) -> dict[str, Any]:
    if min_age_seconds < 60:
        raise ValueError("min_age_seconds must be at least 60")
    if apply and confirmation != CONFIRMATION:
        raise ValueError(f"--apply requires --confirm {CONFIRMATION}")
    unresolved_root = archive_root.expanduser()
    if _is_reparse_point(unresolved_root):
        raise ValueError("archive_root must not be a symlink, junction, or reparse point")
    root = unresolved_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"archive_root is not a directory: {root}")
    timestamp = time.time() if now is None else now
    processes = list(process_provider())
    items = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not child.name.startswith(STAGING_PREFIX):
            continue
        item: dict[str, Any] = {"name": child.name, "path": str(child)}
        try:
            candidate = _validate_direct_staging_child(child, root)
            size_bytes, file_count, newest_mtime = _tree_metrics(candidate)
            age_seconds = max(0.0, timestamp - newest_mtime)
            references = _referencing_processes(candidate, processes)
            item.update(
                {
                    "path": str(candidate),
                    "size_bytes": size_bytes,
                    "file_count": file_count,
                    "newest_mtime": newest_mtime,
                    "age_seconds": round(age_seconds, 3),
                    "process_references": references,
                }
            )
            if age_seconds < min_age_seconds:
                item.update({"status": "skipped", "reason": "younger_than_age_threshold"})
            elif references:
                item.update({"status": "skipped", "reason": "running_process_reference"})
            elif not apply:
                item.update({"status": "eligible", "reason": "dry_run"})
            else:
                # Recheck immediately before the destructive operation.
                candidate = _validate_direct_staging_child(candidate, root)
                fresh_references = _referencing_processes(candidate, process_provider())
                _, _, fresh_mtime = _tree_metrics(candidate)
                if timestamp - fresh_mtime < min_age_seconds:
                    item.update({"status": "skipped", "reason": "became_too_recent"})
                elif fresh_references:
                    item.update(
                        {
                            "status": "skipped",
                            "reason": "running_process_reference",
                            "process_references": fresh_references,
                        }
                    )
                else:
                    shutil.rmtree(candidate, onerror=_remove_readonly)
                    if candidate.exists():
                        raise OSError("staging directory still exists after rmtree")
                    item.update({"status": "removed", "reason": "confirmed_stale_staging"})
        except (OSError, ValueError) as error:
            item.update(
                {
                    "status": "failed",
                    "reason": "safety_validation_or_removal_failed",
                    "detail": f"{type(error).__name__}: {error}"[:1000],
                }
            )
        items.append(item)
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema_version": "paper-code-staging-cleanup/v1",
        "archive_root": str(root),
        "apply": apply,
        "min_age_seconds": min_age_seconds,
        "candidate_prefix": STAGING_PREFIX,
        "counts": dict(sorted(counts.items())),
        "items": items,
    }


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--min-age-seconds", type=int, default=300)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = cleanup_staging(
            archive_root=arguments.archive_root,
            min_age_seconds=arguments.min_age_seconds,
            apply=arguments.apply,
            confirmation=arguments.confirm,
        )
        _atomic_write(
            arguments.report,
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"cleanup-staging failed: {error}")
        return 2
    print(json.dumps({"counts": report["counts"], "report": str(arguments.report.resolve())}))
    return 3 if report["counts"].get("failed") or report["counts"].get("skipped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
