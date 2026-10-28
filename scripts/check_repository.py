"""Offline, read-only checks of tracked documentation and repository hygiene.

This is a deliberately small Markdown link checker, not a CommonMark renderer.
It handles inline/image links, reference definitions and HTML href/src attributes.
Fenced/indented code, inline code, comments and placeholder destinations are ignored.
Fragments are not validated. Neither external links nor external material trees are read.
Local targets must be tracked so the links work in a fresh clone; --files explicitly
allows listed draft Markdown files as targets during editing.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

HISTORY_PREFIXES = ("docs/plans/", "docs/reports/", "docs/superpowers/")
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FIXTURE_BYTES = 1024 * 1024
ARTIFACT_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".dump",
    ".bak",
    ".pt",
    ".pth",
    ".ckpt",
    ".safetensors",
    ".onnx",
    ".bin",
    ".pdf",
    ".zip",
    ".7z",
    ".tar",
    ".gz",
    ".npy",
    ".npz",
    ".parquet",
    ".h5",
    ".hdf5",
}
RUNTIME_PARTS = {"node_modules", ".venv", "__pycache__", ".pytest_cache"}


@dataclass
class Finding:
    severity: str
    code: str
    path: str
    message: str
    line: int | None = None


@dataclass
class Report:
    tracked_files: int = 0
    markdown_files: int = 0
    local_links: int = 0
    skipped_links: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(item.severity == "error" for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "failed" if self.errors else "passed",
            "counts": {
                "tracked_files": self.tracked_files,
                "markdown_files": self.markdown_files,
                "local_links": self.local_links,
                "skipped_links": self.skipped_links,
                "errors": self.errors,
                "warnings": sum(item.severity == "warning" for item in self.findings),
            },
            "findings": [asdict(item) for item in self.findings],
        }


def _mask(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group())


def _prose(text: str) -> str:
    """Mask non-prose while preserving line numbers for diagnostics."""
    text = re.sub(r"<!--.*?-->", _mask, text, flags=re.DOTALL)
    lines: list[str] = []
    fence: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if marker and marker[1][0] == fence and len(marker[1]) >= fence_length:
                fence = None
            lines.append(re.sub(r"[^\n]", " ", line))
        elif marker:
            fence, fence_length = marker[1][0], len(marker[1])
            lines.append(re.sub(r"[^\n]", " ", line))
        elif line.startswith(("    ", "\t")):
            lines.append(re.sub(r"[^\n]", " ", line))
        else:
            lines.append(line)
    return re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", _mask, "".join(lines), flags=re.DOTALL)


def _destination(text: str, start: int) -> str | None:
    """Read a link destination, including balanced parentheses or <space paths>."""
    rest = text[start:].lstrip()
    if rest.startswith("<"):
        end = rest.find(">")
        return rest[1:end] if end >= 0 else None
    depth = 0
    escaped = False
    result = []
    for char in rest:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if not depth:
                break
            depth -= 1
        elif char.isspace() and not depth:
            break
        result.append(char)
    return "".join(result) or None


def markdown_links(text: str) -> Iterator[tuple[int, str]]:
    """Yield (one-based source line, destination); no filesystem access."""
    prose = _prose(text)
    for pattern in (r"(?<!!)\[[^\]\n]*\]\(", r"!\[[^\]\n]*\]\(", r"^ {0,3}\[[^\]\n]+\]:\s*"):
        for match in re.finditer(pattern, prose, flags=re.MULTILINE):
            destination = _destination(prose, match.end())
            if destination:
                yield prose.count("\n", 0, match.start()) + 1, html.unescape(destination)
    for match in re.finditer(
        r"<(?:a|img|source|video|audio)\b[^>]*?\b(?:href|src)\s*=\s*"
        r"([\"'])(.*?)\1",
        prose,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        yield prose.count("\n", 0, match.start()) + 1, html.unescape(match[2])


def tracked_paths(root: Path) -> list[str]:
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
    )
    if Path(top.stdout.decode("utf-8").strip()).resolve() != root:
        raise ValueError("--root must be the Git repository root")
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        check=True,
        capture_output=True,
    )
    return sorted(set(result.stdout.decode("utf-8").strip("\0").split("\0")) - {""})


def _hygiene(root: Path, name: str, report: Report) -> None:
    path = root / name
    # Never follow tracked symlinks into a material tree.
    if not path.resolve().is_relative_to(root):
        report.findings.append(
            Finding(
                "error",
                "tracked_external_path",
                name,
                "Tracked path resolves outside the repository; not read.",
            )
        )
        return
    if not path.is_file():
        report.findings.append(
            Finding(
                "error",
                "tracked_file_missing",
                name,
                "Tracked file is missing or is not a regular file.",
            )
        )
        return
    parts = PurePosixPath(name).parts
    basename = path.name.lower()
    size = path.stat().st_size
    if (
        (basename == ".env" or basename.startswith(".env.")) and not basename.endswith(".example")
    ) or basename in {
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "service-account.json",
    }:
        report.findings.append(
            Finding(
                "error",
                "sensitive_filename",
                name,
                f"Potential secret file must not be tracked ({size} bytes).",
            )
        )
    fixture = name.startswith("tests/fixtures/") and size <= MAX_FIXTURE_BYTES
    if not fixture and (
        path.suffix.lower() in ARTIFACT_SUFFIXES
        or any(part in RUNTIME_PARTS for part in parts)
        or parts[0] in {"work", "data"}
    ):
        report.findings.append(
            Finding(
                "error",
                "runtime_artifact",
                name,
                f"Database, source binary or runtime artifact ({size} bytes).",
            )
        )
    if size > MAX_FILE_BYTES:
        # Existing intake manifests are frozen evidence, not copied source binaries.
        manifest = name.startswith("config/intake/") and basename == "manifest.json"
        report.findings.append(
            Finding(
                "warning" if manifest else "error",
                "large_file",
                name,
                f"File exceeds {MAX_FILE_BYTES} bytes ({size} bytes).",
            )
        )


def check_repository(
    root: Path,
    files: Sequence[str] | None = None,
    *,
    strict_history: bool = False,
) -> Report:
    root = root.resolve()
    tracked = tracked_paths(root)
    available_files = set(tracked)
    tracked_directories = {
        parent.as_posix() for name in tracked for parent in PurePosixPath(name).parents
    }
    if files is not None:
        for name in files:
            draft = (root / name).resolve()
            if draft.is_relative_to(root) and draft.suffix.lower() == ".md" and draft.is_file():
                available_files.add(draft.relative_to(root).as_posix())
    report = Report(tracked_files=len(tracked))
    for name in tracked:
        _hygiene(root, name, report)
    documents = (
        [name for name in tracked if name.lower().endswith(".md")]
        if files is None
        else list(dict.fromkeys(files))
    )
    for name in documents:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            report.findings.append(
                Finding(
                    "error",
                    "document_outside_repository",
                    name,
                    "Explicit document is outside the repository; not read.",
                )
            )
            continue
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() != ".md" or not path.is_file():
            report.findings.append(
                Finding(
                    "error", "document_missing", relative, "Expected an existing Markdown file."
                )
            )
            continue
        report.markdown_files += 1
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeError, OSError) as error:
            report.findings.append(
                Finding(
                    "error",
                    "document_unreadable",
                    relative,
                    f"Cannot read UTF-8 Markdown ({type(error).__name__}).",
                )
            )
            continue
        historical = relative.startswith(HISTORY_PREFIXES)
        severity = "error" if strict_history or not historical else "warning"
        for line, destination in markdown_links(text):
            if any(token in unquote(destination) for token in ("<", ">", "{{", "}}", "${")):
                report.skipped_links += 1
                continue
            try:
                parsed = urlsplit(destination)
            except ValueError:
                report.findings.append(
                    Finding(severity, "invalid_link", relative, "Malformed link destination.", line)
                )
                continue
            if parsed.scheme or parsed.netloc or not parsed.path:
                report.skipped_links += 1
                continue
            decoded = unquote(parsed.path).replace("\\", "/")
            if "\0" in decoded:
                report.findings.append(
                    Finding(severity, "invalid_link", relative, "NUL in link destination.", line)
                )
                continue
            if decoded.startswith("/") or re.match(r"^[a-zA-Z]:", decoded):
                report.skipped_links += 1
                continue
            target = (path.parent / decoded).resolve()
            if not target.is_relative_to(root):
                report.skipped_links += 1
                report.findings.append(
                    Finding(
                        "warning",
                        "external_path_skipped",
                        relative,
                        "Relative link leaves repository; not inspected.",
                        line,
                    )
                )
                continue
            report.local_links += 1
            target_name = target.relative_to(root).as_posix()
            if not target.exists():
                report.findings.append(
                    Finding(
                        severity,
                        "missing_link",
                        relative,
                        f"Missing local target: {target_name}",
                        line,
                    )
                )
            elif (target.is_dir() and target_name not in tracked_directories) or (
                not target.is_dir() and target_name not in available_files
            ):
                report.findings.append(
                    Finding(
                        severity,
                        "untracked_link",
                        relative,
                        f"Target exists but a fresh clone does not include it: {target_name}. "
                        "Track the file/directory contents or explicitly list a draft in --files.",
                        line,
                    )
                )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--files", nargs="+", help="Check only these Markdown paths; allow listed draft targets"
    )
    parser.add_argument(
        "--strict-history",
        action="store_true",
        help="Treat missing/untracked links in plans/reports/superpowers as errors",
    )
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report")
    args = parser.parse_args(argv)
    try:
        report = check_repository(args.root, args.files, strict_history=args.strict_history)
    except (subprocess.CalledProcessError, OSError, UnicodeError, ValueError) as error:
        report = Report(
            findings=[
                Finding(
                    "error",
                    "repository_unreadable",
                    ".",
                    f"Cannot inspect Git repository ({type(error).__name__}).",
                )
            ]
        )
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        for finding in report.findings:
            location = finding.path + (f":{finding.line}" if finding.line else "")
            print(f"{finding.severity.upper()} {finding.code} {location}: {finding.message}")
        print(
            f"Repository check {payload['status']}: "
            + ", ".join(f"{key}={value}" for key, value in payload["counts"].items())
        )
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
