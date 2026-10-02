"""Audit paper/code evidence offline and archive only explicitly approved repositories.

The default mode is read-only and never uses the network.  ``--apply`` is a
separate, fail-closed path: it accepts only a reviewed approval document,
checks out an exact commit with Git LFS smudging disabled, refuses an existing
destination, and records the resulting repository evidence.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

SCHEMA_VERSION = "paper-code-audit/v1"
APPROVAL_SCHEMA_VERSION = "paper-code-approvals/v1"
GITLAB_REPOSITORY_HOSTS = frozenset({"gitlab.com", "gitlab.mpi-sws.org"})
REPOSITORY_HOSTS = frozenset({"github.com", *GITLAB_REPOSITORY_HOSTS})
HTML_CODE_STATUSES = frozenset(
    {
        "official_local",
        "official_url_only",
        "announced_pending",
        "none_found",
        "third_party_only",
        "not_applicable",
        "unknown",
    }
)
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
    }
)
BLOCKED_TREE_COMPONENTS = frozenset(
    {
        "checkpoint",
        "checkpoints",
        "dataset",
        "datasets",
        "pretrained",
        "training-data",
        "training_data",
        "weight",
        "weights",
    }
)
BLOCKED_TREE_SUFFIXES = frozenset(
    {
        ".ckpt",
        ".h5",
        ".hdf5",
        ".npy",
        ".npz",
        ".onnx",
        ".pt",
        ".pth",
        ".safetensors",
    }
)
WINDOWS_RESERVED_COMPONENTS = frozenset({"con", "prn", "aux", "nul"})
WINDOWS_RESERVED_NUMBERED_RE = re.compile(r"^(?:com|lpt)[1-9¹²³]$", re.IGNORECASE)
WINDOWS_INVALID_COMPONENT_CHARS = frozenset('<>:"/\\|?*')
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SSH_REMOTE_RE = re.compile(
    r"^(?:[^@\s]+@)?(?P<host>github\.com|gitlab\.com):(?P<path>[^\s]+)$",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = " ".join(str(value).split()).strip()
    return result or None


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_title(value: Any) -> str:
    text = (_text(value) or "").casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def approved_title_match_kind(exact_title: str, record_title: str) -> str | None:
    """Match a reviewed title to a possibly truncated intake title, never by fuzziness."""

    if exact_title == record_title:
        return "exact"
    exact_key = _normalize_title(exact_title)
    record_key = _normalize_title(record_title)
    if exact_key == record_key:
        return "normalized_exact"
    if len(record_key) >= 40 and exact_key.startswith(record_key):
        return "paper_record_truncated_prefix"
    if len(exact_key) >= 40 and record_key.endswith(exact_key):
        return "paper_record_artifact_prefix"
    common_prefix = 0
    for left, right in zip(exact_key, record_key, strict=False):
        if left != right:
            break
        common_prefix += 1
    if len(record_title) >= 80 and common_prefix >= 32:
        return "paper_record_truncated_long_common_prefix"
    overlap = SequenceMatcher(None, exact_key, record_key, autojunk=False).find_longest_match()
    if len(record_title) >= 80 and overlap.size >= 60 and overlap.a <= 20 and overlap.b <= 20:
        return "paper_record_truncated_long_overlap"
    return None


def _normalize_sha(value: Any) -> str | None:
    text = (_text(value) or "").casefold()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def _fact_value(record: Mapping[str, Any], field_name: str) -> Any:
    facts = record.get("facts")
    if isinstance(facts, Mapping):
        value = facts.get(field_name)
        if isinstance(value, Mapping) and "value" in value:
            return value.get("value")
        if isinstance(value, list):
            return [item.get("value") if isinstance(item, Mapping) else item for item in value]
        if value is not None:
            return value
    return record.get(field_name)


def _is_reparse_point(path: Path) -> bool:
    try:
        result = path.lstat()
    except OSError:
        return False
    attributes = getattr(result, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path.expanduser())))


def _path_candidates(value: Any, *, bases: Sequence[Path]) -> set[str]:
    text = _text(value)
    if not text:
        return set()
    path = Path(text).expanduser()
    if path.is_absolute():
        return {_path_key(path)}
    return {_path_key(base / path) for base in bases}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


@dataclass(frozen=True, slots=True)
class CanonicalLink:
    kind: str
    key: str
    url: str
    clone_url: str | None


def _split_safe_url(value: str) -> SplitResult | None:
    try:
        parsed = urlsplit(value)
        # Accessing hostname/port validates malformed bracket and port syntax.
        _ = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    return parsed


def _url_has_sensitive_or_unstable_parts(value: str) -> bool:
    parsed = _split_safe_url(value)
    if parsed is None:
        return True
    return bool(
        parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port
    )


def canonical_link(value: Any) -> CanonicalLink | None:
    """Return a credential-free canonical repository or project-page link."""

    raw = _text(value)
    if not raw:
        return None
    ssh_match = SSH_REMOTE_RE.fullmatch(raw)
    if ssh_match:
        raw = f"https://{ssh_match.group('host')}/{ssh_match.group('path')}"
    parsed = _split_safe_url(raw)
    if parsed is None or parsed.scheme.casefold() not in {"http", "https", "ssh"}:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in REPOSITORY_HOSTS and not host.endswith((".github.io", ".gitlab.io")):
        return None
    segments = [segment for segment in parsed.path.replace("\\", "/").split("/") if segment]
    if host == "github.com":
        if len(segments) < 2:
            return None
        owner, repository = segments[0], segments[1].removesuffix(".git")
        if not SAFE_SEGMENT_RE.fullmatch(owner) or not SAFE_SEGMENT_RE.fullmatch(repository):
            return None
        url = f"https://github.com/{owner}/{repository}"
        key = f"github.com/{owner}/{repository}".casefold()
        return CanonicalLink("repository", key, url, f"{url}.git")
    if host in GITLAB_REPOSITORY_HOSTS:
        if "-" in segments:
            segments = segments[: segments.index("-")]
        if len(segments) < 2:
            return None
        segments[-1] = segments[-1].removesuffix(".git")
        if not all(SAFE_SEGMENT_RE.fullmatch(segment) for segment in segments):
            return None
        namespace = "/".join(segments)
        url = f"https://{host}/{namespace}"
        return CanonicalLink(
            "repository",
            f"{host}/{namespace}".casefold(),
            url,
            f"{url}.git",
        )
    if len(host.split(".")) < 3:
        return None
    clean_path = "/".join(segments)
    url = urlunsplit(("https", host, f"/{clean_path}" if clean_path else "", "", ""))
    return CanonicalLink("project_page", url.casefold(), url, None)


def _safe_public_url(value: Any) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    link = canonical_link(raw)
    if link is not None:
        return link.url
    parsed = _split_safe_url(raw)
    if parsed is None or parsed.scheme.casefold() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        return None
    return urlunsplit(("https", host, parsed.path or "", "", ""))


@dataclass(slots=True)
class MetadataEvidence:
    source_path: str
    sha256: str | None
    status: str
    title: str | None
    year: int | None
    doi_values: list[str]
    arxiv_values: list[str]
    code_leads: list[dict[str, Any]]
    source_file: str


@dataclass(slots=True)
class PaperEvidence:
    paper_id: str
    title: str
    year: int | None
    doi: str | None
    arxiv_id: str | None
    domains: list[str]
    record_file: str | None
    synthetic: bool = False
    pdf_sha256: str | None = None
    pdf_sha256s: set[str] = field(default_factory=set)
    pdf_path_keys: set[str] = field(default_factory=set)
    code: dict[str, Any] = field(default_factory=dict)
    metadata: list[MetadataEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_document(path: Path) -> list[dict[str, Any]]:
    absolute = path.expanduser()
    if not absolute.exists() or not absolute.is_file():
        raise ValueError(f"input file does not exist: {absolute}")
    if _is_reparse_point(absolute):
        raise ValueError(f"input file is a symlink/reparse point: {absolute}")
    suffix = absolute.suffix.casefold()
    if suffix == ".csv":
        with absolute.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix == ".jsonl":
        result: list[dict[str, Any]] = []
        with absolute.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid JSONL at {absolute}:{line_number}: {error}"
                    ) from error
                if not isinstance(item, dict):
                    raise ValueError(f"JSONL record is not an object at {absolute}:{line_number}")
                result.append(item)
        return result
    try:
        document = json.loads(absolute.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON at {absolute}: {error}") from error
    if isinstance(document, list):
        values = document
    elif isinstance(document, dict):
        values = next(
            (
                document[key]
                for key in ("records", "papers", "items")
                if isinstance(document.get(key), list)
            ),
            [document],
        )
    else:
        raise ValueError(f"JSON input is not an object/list: {absolute}")
    if not all(isinstance(item, dict) for item in values):
        raise ValueError(f"JSON record list contains non-objects: {absolute}")
    return values


def _json_cell(value: Any, *, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    text = _text(value)
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def load_metadata(paths: Sequence[Path]) -> list[MetadataEvidence]:
    """Load the latest extraction revision per PDF path from JSONL or CSV."""

    latest: dict[str, MetadataEvidence] = {}
    for path in paths:
        for raw in _read_document(path):
            source = raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
            source_path = _text(source.get("path") or raw.get("source_path"))
            if not source_path:
                continue
            raw_year = _fact_value(raw, "year")
            try:
                year = int(raw_year) if raw_year not in (None, "") else None
            except (TypeError, ValueError):
                year = None
            code_leads = raw.get("code_leads")
            if not isinstance(code_leads, list):
                code_leads = _json_cell(raw.get("code_leads_json"), default=[])
            doi_values = _items(_fact_value(raw, "doi"))
            if not doi_values:
                doi_values = _items(_json_cell(raw.get("dois_json"), default=[]))
            arxiv_values = _items(_fact_value(raw, "arxiv_id"))
            if not arxiv_values:
                arxiv_values = _items(_json_cell(raw.get("arxiv_ids_json"), default=[]))
            evidence = MetadataEvidence(
                source_path=source_path,
                sha256=_normalize_sha(source.get("sha256") or raw.get("sha256")),
                status=(_text(raw.get("status")) or "unknown").casefold(),
                title=_text(_fact_value(raw, "title")),
                year=year,
                doi_values=[str(value).casefold() for value in doi_values if _text(value)],
                arxiv_values=[str(value).casefold() for value in arxiv_values if _text(value)],
                code_leads=[dict(item) for item in code_leads if isinstance(item, Mapping)],
                source_file=str(path.resolve()),
            )
            latest[_path_key(Path(source_path))] = evidence
    return list(latest.values())


def _paper_code(raw: Mapping[str, Any]) -> dict[str, Any]:
    code = raw.get("code")
    if code is None and isinstance(raw.get("representations"), Mapping):
        representations = raw["representations"]
        code = representations.get("code") or representations.get("repository")
    if isinstance(code, str):
        return {"status": "unknown", "local_path": code}
    return dict(code) if isinstance(code, Mapping) else {"status": "unknown"}


def load_paper_records(paths: Sequence[Path], *, core_root: Path) -> list[PaperEvidence]:
    papers: dict[str, PaperEvidence] = {}
    for path in paths:
        base = path.resolve().parent
        for index, raw in enumerate(_read_document(path), start=1):
            asset = raw.get("asset") if isinstance(raw.get("asset"), Mapping) else raw
            paper_id = _text(
                raw.get("paper_id")
                or raw.get("source_record_id")
                or raw.get("id")
                or asset.get("paper_id")
            )
            title = _text(asset.get("title") or raw.get("title"))
            if not paper_id or not title:
                raise ValueError(f"paper record lacks paper_id/title: {path}:{index}")
            year_value = asset.get("year") or raw.get("year")
            try:
                year = int(year_value) if year_value not in (None, "") else None
            except (TypeError, ValueError):
                year = None
            pdf = raw.get("pdf")
            if pdf is None and isinstance(raw.get("representations"), Mapping):
                pdf = raw["representations"].get("pdf")
            pdf = dict(pdf) if isinstance(pdf, Mapping) else ({"path": pdf} if pdf else {})
            representation_rows = raw.get("pdf_representations")
            if not isinstance(representation_rows, list):
                representation_rows = []
            pdf_sha256s = {
                value
                for value in (
                    _normalize_sha(pdf.get("sha256")),
                    *(
                        _normalize_sha(item.get("sha256"))
                        for item in representation_rows
                        if isinstance(item, Mapping)
                    ),
                )
                if value
            }
            pdf_path_keys = _path_candidates(pdf.get("path"), bases=(base, core_root))
            for candidate_path in (raw.get("source_pdf_path"), raw.get("target_pdf_path")):
                pdf_path_keys.update(_path_candidates(candidate_path, bases=(base, core_root)))
            for item in representation_rows:
                if not isinstance(item, Mapping):
                    continue
                pdf_path_keys.update(_path_candidates(item.get("path"), bases=(base, core_root)))
                pdf_path_keys.update(
                    _path_candidates(item.get("source_pdf_path"), bases=(base, core_root))
                )
            domains = asset.get("domain_slugs") or asset.get("domains") or raw.get("domain_slugs")
            paper = PaperEvidence(
                paper_id=paper_id,
                title=title,
                year=year,
                doi=_text(asset.get("doi") or raw.get("doi")),
                arxiv_id=_text(asset.get("arxiv_id") or raw.get("arxiv_id")),
                domains=sorted({_text(value) for value in _items(domains) if _text(value)}),
                record_file=str(path.resolve()),
                pdf_sha256=_normalize_sha(pdf.get("sha256")),
                pdf_sha256s=pdf_sha256s,
                pdf_path_keys=pdf_path_keys,
                code=_paper_code(raw),
            )
            previous = papers.get(paper_id)
            if previous is not None:
                if _normalize_title(previous.title) != _normalize_title(title):
                    raise ValueError(f"duplicate paper_id with conflicting title: {paper_id}")
                previous.pdf_path_keys.update(paper.pdf_path_keys)
                previous.pdf_sha256s.update(paper.pdf_sha256s)
                previous.pdf_sha256 = previous.pdf_sha256 or paper.pdf_sha256
                if previous.code.get("status", "unknown") == "unknown":
                    previous.code = paper.code
                continue
            papers[paper_id] = paper
    return list(papers.values())


def _paper_identifiers(paper: PaperEvidence) -> set[str]:
    result = set()
    if paper.doi:
        result.add(f"doi:{paper.doi.casefold()}")
    if paper.arxiv_id:
        result.add(f"arxiv:{paper.arxiv_id.casefold().removesuffix('.pdf')}")
    return result


def attach_metadata(
    papers: list[PaperEvidence], metadata: Sequence[MetadataEvidence]
) -> list[PaperEvidence]:
    """Attach only strong path/hash/identifier matches; title matches stay warnings."""

    by_sha: dict[str, set[str]] = defaultdict(set)
    by_path: dict[str, set[str]] = defaultdict(set)
    by_identifier: dict[str, set[str]] = defaultdict(set)
    by_id = {paper.paper_id: paper for paper in papers}
    for paper in papers:
        for digest in paper.pdf_sha256s or ({paper.pdf_sha256} if paper.pdf_sha256 else set()):
            by_sha[digest].add(paper.paper_id)
        for key in paper.pdf_path_keys:
            by_path[key].add(paper.paper_id)
        for identifier in _paper_identifiers(paper):
            by_identifier[identifier].add(paper.paper_id)

    for item in metadata:
        matches: set[str] = set()
        if item.sha256:
            matches.update(by_sha.get(item.sha256, set()))
        matches.update(by_path.get(_path_key(Path(item.source_path)), set()))
        for value in item.doi_values:
            matches.update(by_identifier.get(f"doi:{value}", set()))
        for value in item.arxiv_values:
            matches.update(by_identifier.get(f"arxiv:{value}", set()))
        if len(matches) == 1:
            by_id[next(iter(matches))].metadata.append(item)
            continue
        digest = (
            item.sha256
            or hashlib.sha256(_path_key(Path(item.source_path)).encode("utf-8")).hexdigest()
        )
        title = item.title or Path(item.source_path).stem
        synthetic_id = f"pdf-sha256-{digest}"
        if synthetic_id in by_id:
            path_digest = hashlib.sha256(
                _path_key(Path(item.source_path)).encode("utf-8")
            ).hexdigest()
            synthetic_id = f"{synthetic_id}-copy-{path_digest[:16]}"
        synthetic = PaperEvidence(
            paper_id=synthetic_id,
            title=title,
            year=item.year,
            doi=item.doi_values[0] if len(item.doi_values) == 1 else None,
            arxiv_id=item.arxiv_values[0] if len(item.arxiv_values) == 1 else None,
            domains=[],
            record_file=None,
            synthetic=True,
            pdf_sha256=item.sha256,
            pdf_path_keys={_path_key(Path(item.source_path))},
            metadata=[item],
        )
        if matches:
            synthetic.warnings.append(
                "metadata_strong_match_ambiguous:" + ",".join(sorted(matches))
            )
        else:
            weak = [
                paper.paper_id
                for paper in papers
                if _normalize_title(paper.title) == _normalize_title(title)
                and (item.year is None or paper.year is None or item.year == paper.year)
            ]
            if weak:
                synthetic.warnings.append(
                    "metadata_title_match_requires_review:" + ",".join(sorted(weak))
                )
        papers.append(synthetic)
        by_id[synthetic.paper_id] = synthetic
    return papers


def _git_config_path(repository: Path, *, allowed_roots: Sequence[Path]) -> Path | None:
    marker = repository / ".git"
    if marker.is_dir():
        return marker / "config"
    if not marker.is_file() or _is_reparse_point(marker):
        return None
    try:
        content = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not content.casefold().startswith("gitdir:"):
        return None
    git_dir = Path(content.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = repository / git_dir
    if not any(_within(git_dir, root) for root in allowed_roots):
        return None
    return git_dir / "config"


def _config_remotes(config_path: Path) -> list[dict[str, str]]:
    if not config_path.is_file() or _is_reparse_point(config_path):
        return []
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read(config_path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError):
        return []
    result = []
    for section in parser.sections():
        match = re.fullmatch(r'remote "(.+)"', section)
        if not match or not parser.has_option(section, "url"):
            continue
        link = canonical_link(parser.get(section, "url"))
        if link is None or link.kind != "repository":
            continue
        result.append({"name": match.group(1), "url": link.url, "key": link.key})
    return result


def scan_repositories(roots: Sequence[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    """Inventory Git remotes without following symlinks, junctions, or dependencies."""

    resolved_roots: list[Path] = []
    warnings: list[str] = []
    for root in roots:
        absolute = root.expanduser().resolve(strict=True)
        if not absolute.is_dir():
            raise ValueError(f"repository root is not a directory: {absolute}")
        if _is_reparse_point(root.expanduser()):
            raise ValueError(f"repository root is a symlink/reparse point: {root}")
        resolved_roots.append(absolute)
    repositories: dict[str, dict[str, Any]] = {}
    for root in resolved_roots:
        for current, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            kept = []
            for name in dirnames:
                child = current_path / name
                if (
                    name.casefold() in SKIP_DIRS
                    or name.casefold().startswith(".paper-code-audit-")
                    or _is_reparse_point(child)
                ):
                    continue
                kept.append(name)
            dirnames[:] = kept
            config_path = _git_config_path(current_path, allowed_roots=resolved_roots)
            if config_path is None:
                continue
            remotes = _config_remotes(config_path)
            if not remotes:
                warnings.append(f"repository_without_supported_remote:{current_path}")
            repositories[_path_key(current_path)] = {
                "path": str(current_path.resolve()),
                "remotes": remotes,
                "canonical_keys": sorted({item["key"] for item in remotes}),
            }
            # A repository is one archive unit.  Do not crawl its source tree or vendored deps.
            dirnames[:] = []
    return sorted(repositories.values(), key=lambda item: item["path"].casefold()), warnings


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run_command(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    input_text: str | None = None,
) -> CommandResult:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=environment,
            capture_output=True,
            input=input_text,
            text=True,
            errors="replace",
            shell=False,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CommandResult(127, "", f"{type(error).__name__}: {error}")
    return CommandResult(
        completed.returncode,
        completed.stdout.rstrip("\r\n"),
        completed.stderr.strip(),
    )


def _license_evidence(repository: Path) -> dict[str, Any]:
    files = []
    detected = set()
    patterns = {
        "mit license": "MIT",
        "apache license": "Apache",
        "gnu general public license": "GPL",
        "gnu affero general public license": "AGPL",
        "mozilla public license": "MPL",
        "redistribution and use in source and binary forms": "BSD",
    }
    try:
        candidates = list(repository.iterdir())
    except OSError:
        candidates = []
    for candidate in candidates:
        folded = candidate.name.casefold()
        if not candidate.is_file() or not folded.startswith(("license", "copying", "notice")):
            continue
        try:
            payload = candidate.read_bytes()[:131_072]
        except OSError:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        text = payload.decode("utf-8", errors="ignore").casefold()
        for needle, label in patterns.items():
            if needle in text:
                detected.add(label)
        files.append({"path": candidate.name, "sha256_prefix": digest[:16]})
    return {
        "status": sorted(detected)[0] if len(detected) == 1 else "not-declared",
        "detected_labels": sorted(detected),
        "files": sorted(files, key=lambda item: item["path"].casefold()),
    }


def _git_output(
    repository: Path,
    *arguments: str,
    timeout: int = 60,
    input_text: str | None = None,
) -> CommandResult:
    return _run_command(
        ("git", "-C", str(repository), *arguments),
        timeout=timeout,
        input_text=input_text,
    )


def inspect_repository(repository: Path, *, core_root: Path) -> dict[str, Any]:
    commit_result = _git_output(repository, "rev-parse", "--verify", "HEAD")
    commit = commit_result.stdout.casefold() if COMMIT_RE.fullmatch(commit_result.stdout) else None
    status_result = _git_output(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
    )
    allowed_excluded_deletions: list[str] = []
    disallowed_dirty: list[str] = []
    if status_result.returncode == 0:
        auto_excluded_paths = []
        if status_result.stdout:
            tree_result = _git_output(repository, "ls-tree", "-r", "-z", "--name-only", "HEAD")
            if tree_result.returncode == 0:
                try:
                    auto_excluded_paths = list(
                        _build_sparse_checkout_plan(
                            _parse_tree_paths(tree_result.stdout),
                            explicit_sparse_paths=[],
                        ).auto_excluded_paths
                    )
                except ValueError:
                    auto_excluded_paths = []
        allowed_excluded_deletions, disallowed_dirty = _partition_dirty_entries(
            status_result.stdout,
            auto_excluded_paths=auto_excluded_paths,
        )
        dirty: bool | None = bool(disallowed_dirty)
    else:
        dirty = None
    remote_result = _git_output(repository, "remote", "-v")
    remotes: dict[str, dict[str, str]] = {}
    if remote_result.returncode == 0:
        for line in remote_result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 2:
                continue
            link = canonical_link(fields[1])
            if link and link.kind == "repository":
                remotes[f"{fields[0]}:{link.key}"] = {
                    "name": fields[0],
                    "url": link.url,
                    "key": link.key,
                }
    gitmodules = repository / ".gitmodules"
    submodules: list[str] = []
    if gitmodules.is_file() and not _is_reparse_point(gitmodules):
        try:
            for line in gitmodules.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.match(r"\s*path\s*=", line):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        submodules.append(value)
        except OSError:
            pass
    lfs_patterns: list[str] = []
    attributes = repository / ".gitattributes"
    if attributes.is_file() and not _is_reparse_point(attributes):
        try:
            lfs_patterns = sorted(
                line.split()[0]
                for line in attributes.read_text(encoding="utf-8", errors="replace").splitlines()
                if "filter=lfs" in line and line.split()
            )
        except OSError:
            pass
    # `git lfs ls-files` may ask a promisor remote for blob metadata in a
    # partial clone.  Infer tracked LFS paths from .gitattributes plus the
    # already-local index instead; this remains offline and deterministic.
    tracked_result = _git_output(repository, "ls-files", "-z")
    tracked_files = [item for item in tracked_result.stdout.split("\0") if item]
    lfs_files = sorted(
        path
        for path in tracked_files
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in lfs_patterns)
    )
    return {
        "path": str(repository.resolve()),
        "inside_core": _within(repository, core_root),
        "commit": commit,
        "dirty": dirty,
        "allowed_excluded_deletions": allowed_excluded_deletions,
        "disallowed_dirty_entries": disallowed_dirty[:100],
        "remotes": sorted(remotes.values(), key=lambda item: (item["name"], item["key"])),
        "license": _license_evidence(repository),
        "submodules": sorted(set(submodules)),
        "lfs": {
            "smudge_performed_by_tool": False,
            "files": lfs_files,
            "patterns": lfs_patterns,
            "command_available": tracked_result.returncode == 0,
            "inspection_method": "gitattributes+git-ls-files",
        },
        "inspection_errors": [
            message
            for message in (
                None if commit_result.returncode == 0 else "commit_unreadable",
                None if status_result.returncode == 0 else "dirty_state_unreadable",
                None if remote_result.returncode == 0 else "remote_unreadable",
            )
            if message
        ],
    }


@dataclass(frozen=True, slots=True)
class ApprovedPaper:
    paper_id: str
    exact_title: str


@dataclass(frozen=True, slots=True)
class RepositoryApproval:
    link: CanonicalLink
    commit: str
    destination: str
    papers: tuple[ApprovedPaper, ...]
    sparse_paths: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NoCodeApproval:
    paper_id: str
    exact_title: str
    reviewed_at: str
    evidence_urls: tuple[str, ...]


@dataclass(slots=True)
class ApprovalSet:
    repositories: dict[str, RepositoryApproval] = field(default_factory=dict)
    no_code: dict[str, NoCodeApproval] = field(default_factory=dict)


def _safe_relative_path(value: Any, *, label: str) -> str:
    text = _text(value)
    if not text or "\\" in text or "\0" in text or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{label} must be a non-empty portable relative path")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} escapes its archive root: {text}")
    return path.as_posix()


def load_approvals(path: Path | None, papers: Sequence[PaperEvidence]) -> ApprovalSet:
    if path is None:
        return ApprovalSet()
    raw_records = _read_document(path)
    if len(raw_records) != 1:
        raise ValueError("approval document must be one JSON object")
    document = raw_records[0]
    if document.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        raise ValueError(f"approval schema must be {APPROVAL_SCHEMA_VERSION}")
    paper_by_id = {paper.paper_id: paper for paper in papers}
    result = ApprovalSet()
    for index, raw in enumerate(_items(document.get("repositories")), start=1):
        if not isinstance(raw, Mapping) or raw.get("official") is not True:
            raise ValueError(f"repository approval {index} must set official=true")
        url = _text(raw.get("url")) or ""
        if _url_has_sensitive_or_unstable_parts(url):
            raise ValueError(f"repository approval {index} URL contains credentials/query/fragment")
        parsed_url = _split_safe_url(url)
        if parsed_url is None or parsed_url.scheme.casefold() != "https":
            raise ValueError(f"repository approval {index} URL must use clean HTTPS")
        link = canonical_link(url)
        if link is None or link.kind != "repository" or link.clone_url is None:
            raise ValueError(f"repository approval {index} is not a GitHub/GitLab repository")
        commit = (_text(raw.get("commit")) or "").casefold()
        if not COMMIT_RE.fullmatch(commit):
            raise ValueError(f"repository approval {index} requires an exact 40-hex commit")
        destination = _safe_relative_path(
            raw.get("destination"), label=f"repository approval {index} destination"
        )
        sparse_paths = tuple(
            _safe_relative_path(value, label=f"repository approval {index} sparse path")
            for value in _items(raw.get("sparse_paths"))
        )
        approved_papers = []
        for paper_raw in _items(raw.get("papers")):
            if not isinstance(paper_raw, Mapping):
                raise ValueError(f"repository approval {index} paper entry is not an object")
            paper_id = _text(paper_raw.get("paper_id")) or ""
            exact_title = _text(paper_raw.get("exact_title")) or ""
            paper = paper_by_id.get(paper_id)
            title_match = _text(paper_raw.get("title_match"))
            if paper is None or (
                approved_title_match_kind(exact_title, paper.title) is None
                and title_match != "agent_reviewed_exact_title_override"
            ):
                message = (
                    f"repository approval {index} paper/title does not match "
                    f"current input: {paper_id}"
                )
                raise ValueError(message)
            approved_papers.append(ApprovedPaper(paper_id, exact_title))
        if not approved_papers:
            raise ValueError(f"repository approval {index} has no papers")
        evidence = tuple(_text(value) or "" for value in _items(raw.get("evidence")))
        if not all(evidence):
            raise ValueError(f"repository approval {index} requires non-empty evidence")
        approval = RepositoryApproval(
            link=link,
            commit=commit,
            destination=destination,
            papers=tuple(approved_papers),
            sparse_paths=sparse_paths,
            evidence=evidence,
        )
        previous = result.repositories.get(link.key)
        if previous is not None and previous != approval:
            raise ValueError(f"conflicting repository approvals for {link.url}")
        result.repositories[link.key] = approval

    for index, raw in enumerate(_items(document.get("no_code")), start=1):
        if not isinstance(raw, Mapping) or raw.get("verified_no_public_code") is not True:
            raise ValueError(f"no_code approval {index} must set verified_no_public_code=true")
        paper_id = _text(raw.get("paper_id")) or ""
        exact_title = _text(raw.get("exact_title")) or ""
        paper = paper_by_id.get(paper_id)
        title_match = _text(raw.get("title_match"))
        if paper is None or (
            approved_title_match_kind(exact_title, paper.title) is None
            and title_match != "agent_reviewed_exact_title_override"
        ):
            raise ValueError(f"no_code approval {index} title does not exactly match: {paper_id}")
        reviewed_at = _text(raw.get("reviewed_at")) or ""
        evidence_urls = tuple(
            url
            for value in _items(raw.get("evidence_urls"))
            if (url := _safe_public_url(value)) is not None
        )
        if not reviewed_at or not evidence_urls:
            raise ValueError(f"no_code approval {index} requires reviewed_at and evidence_urls")
        result.no_code[paper_id] = NoCodeApproval(
            paper_id=paper_id,
            exact_title=exact_title,
            reviewed_at=reviewed_at,
            evidence_urls=evidence_urls,
        )
    return result


def _metadata_candidates(paper: PaperEvidence) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for metadata in paper.metadata:
        for lead in metadata.code_leads:
            urls = []
            if lead.get("url"):
                urls.append(lead.get("url"))
            urls.extend(_items(lead.get("urls")))
            for value in urls:
                link = canonical_link(value)
                if link is None:
                    continue
                key = (link.kind, link.key)
                confidence_value = lead.get("confidence")
                try:
                    confidence = float(confidence_value)
                except (TypeError, ValueError):
                    confidence = None
                evidence_sources = []
                for item in _items(lead.get("evidence")):
                    if isinstance(item, Mapping):
                        evidence_sources.append(
                            {
                                "source": _text(item.get("source")),
                                "location": _text(item.get("location")),
                            }
                        )
                candidate = {
                    "kind": link.kind,
                    "url": link.url,
                    "canonical_key": link.key,
                    "status": "candidate_unverified",
                    "confidence": confidence,
                    "lead_type": _text(lead.get("type")) or "unknown",
                    "pdf_path": metadata.source_path,
                    "evidence": evidence_sources,
                }
                previous = result.get(key)
                if previous is None or (confidence or 0.0) > (previous.get("confidence") or 0.0):
                    result[key] = candidate
    code_url = paper.code.get("url")
    link = canonical_link(code_url)
    structured_status = (_text(paper.code.get("status")) or "unknown").casefold()
    if link is not None and structured_status not in {"official_local", "official_url_only"}:
        result[(link.kind, link.key)] = {
            "kind": link.kind,
            "url": link.url,
            "canonical_key": link.key,
            "status": "candidate_unverified",
            "confidence": None,
            "lead_type": "paper_record_unverified_code_url",
            "pdf_path": None,
            "evidence": [{"source": "paper_record", "location": paper.record_file}],
        }
    return sorted(result.values(), key=lambda item: (item["kind"], item["canonical_key"]))


def _repository_index(repositories: Sequence[Mapping[str, Any]]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = defaultdict(list)
    for repository in repositories:
        path = Path(str(repository["path"]))
        for key in _items(repository.get("canonical_keys")):
            result[str(key)].append(path)
    return result


def _html_api_mapping(status: str, *, title: str) -> dict[str, Any]:
    if status == "official_local":
        return {
            "html_code_status": "official_local",
            "hub_repository_availability": "available",
            "api_code_action_expected": "available",
            "check_unit_actions_acceptable": True,
            "allowlist_exact_title_candidate": None,
        }
    if status == "not_provided":
        return {
            "html_code_status": "none_found",
            "hub_repository_availability": None,
            "api_code_action_expected": "not_provided",
            "check_unit_actions_acceptable": True,
            "allowlist_exact_title_candidate": title,
        }
    html_status = status if status in HTML_CODE_STATUSES else "unknown"
    return {
        "html_code_status": html_status,
        "hub_repository_availability": None,
        "api_code_action_expected": None,
        "check_unit_actions_acceptable": False,
        "allowlist_exact_title_candidate": None,
    }


def _paper_audit(
    paper: PaperEvidence,
    *,
    repository_index: Mapping[str, list[Path]],
    approvals: ApprovalSet,
    inspection_cache: dict[str, dict[str, Any]],
    core_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates = _metadata_candidates(paper)
    for candidate in candidates:
        candidate["existing_local_paths"] = sorted(
            str(path.resolve()) for path in repository_index.get(candidate["canonical_key"], [])
        )
    structured_status = (_text(paper.code.get("status")) or "unknown").casefold()
    issues = list(paper.warnings)
    official_links: dict[str, CanonicalLink] = {}
    structured_link = canonical_link(paper.code.get("url"))
    if structured_status in {"official_local", "official_url_only"}:
        if structured_link is None:
            issues.append("structured_official_status_without_supported_url")
        else:
            official_links[structured_link.key] = structured_link
    for approval in approvals.repositories.values():
        if any(item.paper_id == paper.paper_id for item in approval.papers):
            official_links[approval.link.key] = approval.link

    local_matches = []
    expected_commits = {
        key: approval.commit
        for key, approval in approvals.repositories.items()
        if any(item.paper_id == paper.paper_id for item in approval.papers)
    }
    declared_commit = (_text(paper.code.get("commit")) or "").casefold()
    if declared_commit and not COMMIT_RE.fullmatch(declared_commit):
        issues.append("paper_record_commit_is_not_exact_40_hex")
        declared_commit = ""
    for key, link in official_links.items():
        for path in repository_index.get(key, []):
            path_key = _path_key(path)
            if path_key not in inspection_cache:
                inspection_cache[path_key] = inspect_repository(path, core_root=core_root)
            inspection = inspection_cache[path_key]
            license_status = inspection["license"]["status"]
            declared_license = _text(paper.code.get("license"))
            if license_status == "not-declared" and declared_license:
                license_status = declared_license
            expected = expected_commits.get(key) or declared_commit or None
            ready = bool(
                inspection["inside_core"]
                and inspection["commit"]
                and inspection["dirty"] is False
                and (expected is None or inspection["commit"] == expected)
                and license_status
            )
            local_matches.append(
                {
                    "canonical_key": key,
                    "url": link.url,
                    "path": inspection["path"],
                    "ready": ready,
                    "expected_commit": expected,
                    "evidence": inspection,
                    "effective_license": license_status,
                }
            )
    ready_matches = [item for item in local_matches if item["ready"]]
    no_code = approvals.no_code.get(paper.paper_id)
    if ready_matches:
        status = "official_local"
        hard_block = False
        selected = sorted(ready_matches, key=lambda item: item["path"].casefold())[0]
        if len(ready_matches) > 1:
            issues.append("multiple_local_archives_reuse_same_official_remote")
    elif official_links:
        status = "official_url_only"
        hard_block = True
        selected = None
        issues.append("official_url_only_is_a_completion_blocker")
        if local_matches:
            issues.append("local_repository_failed_fixed_commit_cleanliness_or_location_checks")
    elif no_code is not None:
        status = "not_provided"
        hard_block = False
        selected = None
    elif structured_status == "announced_pending":
        status = "announced_pending"
        hard_block = True
        selected = None
    elif structured_status == "third_party_only":
        status = "third_party_only"
        hard_block = True
        selected = None
    elif candidates:
        status = "candidate_unverified"
        hard_block = True
        selected = None
    else:
        status = "unknown"
        hard_block = True
        selected = None

    queue = []
    if status == "official_url_only":
        for link in official_links.values():
            queue.append(
                {
                    "paper_id": paper.paper_id,
                    "action": (
                        "archive_official_repository"
                        if link.kind == "repository"
                        else "resolve_official_project_page"
                    ),
                    "url": link.url,
                    "priority": "blocking",
                    "reason": "official_url_only must become a clean, fixed local archive",
                }
            )
    elif status == "candidate_unverified":
        for candidate in candidates:
            queue.append(
                {
                    "paper_id": paper.paper_id,
                    "action": (
                        "verify_official_repository"
                        if candidate["kind"] == "repository"
                        else "resolve_project_page"
                    ),
                    "url": candidate["url"],
                    "priority": "review",
                    "reason": "offline PDF extraction cannot prove official ownership",
                }
            )
    elif status == "announced_pending":
        queue.append(
            {
                "paper_id": paper.paper_id,
                "action": "monitor_announced_code",
                "url": None,
                "priority": "blocking",
                "reason": "paper announces code but no public archive is available",
            }
        )
    elif status == "unknown":
        queue.append(
            {
                "paper_id": paper.paper_id,
                "action": "search_official_code",
                "url": None,
                "priority": "review",
                "reason": "no reviewed official-code or reviewed no-code decision exists",
            }
        )

    output = {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "year": paper.year,
        "domains": paper.domains,
        "synthetic_metadata_record": paper.synthetic,
        "metadata_pdf_paths": sorted({item.source_path for item in paper.metadata}),
        "status": status,
        "hard_block": hard_block,
        "structured_input_status": structured_status,
        "official_urls": sorted(link.url for link in official_links.values()),
        "candidate_leads": candidates,
        "local_matches": local_matches,
        "selected_local_archive": selected,
        "no_code_review": asdict(no_code) if no_code is not None else None,
        "html_api_mapping": _html_api_mapping(status, title=paper.title),
        "issues": sorted(set(issues)),
    }
    return output, queue


def _tree_entry_path(line: str) -> str | None:
    # Accept both legacy --long output and the preferred --name-only output.
    # --long asks a partial clone for blob sizes and may fetch every promised
    # blob, defeating the lightweight archive policy.
    value = line.split("\t", 1)[1] if "\t" in line else line
    value = value.strip()
    return value or None


def _selected_by_sparse(path: str, sparse_paths: Sequence[str]) -> bool:
    if not sparse_paths:
        return True
    pure = PurePosixPath(path)
    if len(pure.parts) == 1:  # cone mode also materializes root files
        return True
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in sparse_paths
    )


def _blocked_tree_paths(lines: Iterable[str], *, sparse_paths: Sequence[str]) -> list[str]:
    blocked = []
    for line in lines:
        path = _tree_entry_path(line)
        if not path or not _selected_by_sparse(path, sparse_paths):
            continue
        pure = PurePosixPath(path)
        if any(part.casefold() in BLOCKED_TREE_COMPONENTS for part in pure.parts[:-1]):
            blocked.append(path)
        elif pure.suffix.casefold() in BLOCKED_TREE_SUFFIXES:
            blocked.append(path)
    return sorted(set(blocked))


@dataclass(frozen=True, slots=True)
class SparseCheckoutPlan:
    patterns: tuple[str, ...]
    checkout_paths: tuple[str, ...]
    auto_excluded_paths: tuple[str, ...]
    casefold_collision_paths: tuple[str, ...]
    casefold_collision_winners: tuple[tuple[str, str], ...]
    auto_exclusion_reasons: tuple[tuple[str, tuple[str, ...]], ...]
    selected_path_count: int


@dataclass(slots=True)
class _SparseNode:
    terminal_kind: str | None = None
    children: dict[str, _SparseNode] = field(default_factory=dict)


def _parse_tree_paths(output: str) -> list[str]:
    raw_paths = output.split("\0") if "\0" in output else output.splitlines()
    paths = []
    for raw in raw_paths:
        if raw == "":
            continue
        path = raw
        if path.startswith("/") or path in {".", ".."} or path.startswith("../"):
            raise ValueError(f"unsafe Git tree path: {path!r}")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError(f"unsafe Git tree path components: {path!r}")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ValueError("Git tree contains duplicate path names")
    return sorted(paths)


def _windows_component_reasons(component: str) -> list[str]:
    reasons = []
    if component.endswith((".", " ")):
        reasons.append("windows_trailing_dot_or_space")
    if any(character in WINDOWS_INVALID_COMPONENT_CHARS for character in component):
        reasons.append("windows_invalid_character")
    if any(ord(character) < 32 for character in component):
        reasons.append("windows_control_character")
    trimmed = component.rstrip(" .")
    stem = trimmed.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED_COMPONENTS or WINDOWS_RESERVED_NUMBERED_RE.fullmatch(stem):
        reasons.append("windows_reserved_name")
    if len(component.encode("utf-16-le")) // 2 > 255:
        reasons.append("windows_component_too_long")
    return reasons


def _unsafe_tree_path(path: str) -> tuple[list[str], str, bool] | None:
    parts = path.split("/")
    reasons = []
    exclusion_path = path
    exclusion_is_directory = False
    exclusion_chosen = False
    for index, component in enumerate(parts):
        is_directory = index < len(parts) - 1
        component_reasons = _windows_component_reasons(component)
        if is_directory and component.casefold() in BLOCKED_TREE_COMPONENTS:
            component_reasons.append("blocked_tree_component")
        if component_reasons:
            reasons.extend(component_reasons)
            if not exclusion_chosen:
                exclusion_path = "/".join(parts[: index + 1])
                exclusion_is_directory = is_directory
                exclusion_chosen = True
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in BLOCKED_TREE_SUFFIXES:
        reasons.append("blocked_tree_suffix")
        if not exclusion_chosen:
            exclusion_path = path
            exclusion_is_directory = False
            exclusion_chosen = True
    # The working tree is nested below a long core/archive/staging prefix.
    # Reserve headroom for that prefix; checking only the relative 220-char
    # threshold still lets Git silently omit tracked paths on Windows.
    if len(path.encode("utf-16-le")) // 2 > 120:
        reasons.append("windows_relative_path_too_long")
        if not exclusion_chosen:
            exclusion_path = path
            exclusion_is_directory = False
            exclusion_chosen = True
    if not reasons:
        return None
    return sorted(set(reasons)), exclusion_path, exclusion_is_directory


def _escape_sparse_component(component: str) -> str:
    if any(character in component for character in ("\0", "\n", "\r")):
        raise ValueError(f"path cannot be represented in sparse-checkout patterns: {component!r}")
    result = []
    for character in component:
        if character in {"\\", "*", "?", "[", "]", "!", "#", " "}:
            result.append("\\")
        result.append(character)
    return "".join(result)


def _escape_sparse_path(path: str) -> str:
    return "/".join(_escape_sparse_component(part) for part in path.split("/"))


def _insert_sparse_target(root: _SparseNode, path: str, *, kind: str) -> None:
    node = root
    for component in path.split("/"):
        node = node.children.setdefault(component, _SparseNode())
    if node.terminal_kind is not None and node.terminal_kind != kind:
        raise ValueError(f"sparse path is both a file and directory: {path}")
    node.terminal_kind = kind


def _positive_sparse_patterns(root: _SparseNode) -> list[str]:
    patterns = ["/*", "!/*"]

    def emit(node: _SparseNode, prefix: tuple[str, ...]) -> None:
        for component, child in sorted(node.children.items(), key=lambda item: item[0]):
            child_prefix = (*prefix, component)
            path = "/".join(_escape_sparse_component(part) for part in child_prefix)
            if child.terminal_kind == "file":
                patterns.append(f"/{path}")
                continue
            patterns.append(f"/{path}/")
            if child.terminal_kind == "directory":
                continue
            patterns.append(f"!/{path}/*")
            emit(child, child_prefix)

    emit(root, ())
    return patterns


def _minimal_exclusions(
    exclusions: Iterable[tuple[str, bool]],
) -> list[tuple[str, bool]]:
    result: list[tuple[str, bool]] = []
    ordered = sorted(set(exclusions), key=lambda item: (len(item[0].split("/")), item[0]))
    for path, is_directory in ordered:
        if any(
            parent_is_directory and (path == parent or path.startswith(parent + "/"))
            for parent, parent_is_directory in result
        ):
            continue
        result.append((path, is_directory))
    return result


def _build_sparse_checkout_plan(
    tree_paths: Sequence[str], *, explicit_sparse_paths: Sequence[str]
) -> SparseCheckoutPlan:
    tree_set = set(tree_paths)
    for sparse_path in explicit_sparse_paths:
        if sparse_path not in tree_set and not any(
            path.startswith(sparse_path.rstrip("/") + "/") for path in tree_paths
        ):
            raise ValueError(f"approved sparse path does not exist at fixed commit: {sparse_path}")

    if explicit_sparse_paths:
        root = _SparseNode()
        for path in tree_paths:
            if "/" not in path:  # preserve repository-level license/config/readme files
                _insert_sparse_target(root, path, kind="file")
        for sparse_path in explicit_sparse_paths:
            is_directory = any(
                path.startswith(sparse_path.rstrip("/") + "/") for path in tree_paths
            )
            _insert_sparse_target(
                root,
                sparse_path,
                kind="directory" if is_directory else "file",
            )
        patterns = _positive_sparse_patterns(root)
    else:
        patterns = ["/*"]

    selected_paths = [
        path for path in tree_paths if _selected_by_sparse(path, explicit_sparse_paths)
    ]
    by_casefold: dict[str, list[str]] = defaultdict(list)
    for path in selected_paths:
        by_casefold[path.casefold()].append(path)
    collision_losers = {
        path
        for candidates in by_casefold.values()
        for path in sorted(candidates)[1:]
        if len(candidates) > 1
    }
    collision_winners = {
        path: sorted(candidates)[0]
        for candidates in by_casefold.values()
        for path in sorted(candidates)[1:]
        if len(candidates) > 1
    }

    excluded_paths = []
    exclusion_reasons = []
    exclusion_patterns = []
    for path in tree_paths:
        if not _selected_by_sparse(path, explicit_sparse_paths):
            continue
        unsafe = _unsafe_tree_path(path)
        is_collision_loser = path in collision_losers
        if unsafe is None and not is_collision_loser:
            continue
        if unsafe is None:
            reasons, exclusion_path, exclusion_is_directory = [], path, False
        else:
            reasons, exclusion_path, exclusion_is_directory = unsafe
        if is_collision_loser:
            reasons = sorted({*reasons, "windows_casefold_collision"})
        excluded_paths.append(path)
        exclusion_reasons.append((path, tuple(reasons)))
        exclusion_patterns.append((exclusion_path, exclusion_is_directory))
    for path, is_directory in _minimal_exclusions(exclusion_patterns):
        escaped = _escape_sparse_path(path)
        patterns.append(f"!/{escaped}/" if is_directory else f"!/{escaped}")
    excluded_set = set(excluded_paths)
    checkout_paths = tuple(
        sorted(
            path
            for path in tree_paths
            if _selected_by_sparse(path, explicit_sparse_paths) and path not in excluded_set
        )
    )
    return SparseCheckoutPlan(
        patterns=tuple(dict.fromkeys(patterns)),
        checkout_paths=checkout_paths,
        auto_excluded_paths=tuple(sorted(set(excluded_paths))),
        casefold_collision_paths=tuple(sorted(collision_losers)),
        casefold_collision_winners=tuple(sorted(collision_winners.items())),
        auto_exclusion_reasons=tuple(sorted(set(exclusion_reasons))),
        selected_path_count=sum(
            _selected_by_sparse(path, explicit_sparse_paths) for path in tree_paths
        ),
    )


def _configure_non_cone_sparse_checkout(
    repository: Path, plan: SparseCheckoutPlan
) -> CommandResult:
    for key, value in (
        ("core.autocrlf", "false"),
        ("core.eol", "lf"),
        ("core.ignoreCase", "false"),
        ("core.sparseCheckout", "true"),
        ("core.sparseCheckoutCone", "false"),
        ("index.sparse", "false"),
    ):
        configured = _git_output(repository, "config", key, value)
        if configured.returncode != 0:
            return configured
    git_dir_result = _git_output(repository, "rev-parse", "--git-dir")
    if git_dir_result.returncode != 0:
        return git_dir_result
    git_dir = Path(git_dir_result.stdout)
    if not git_dir.is_absolute():
        git_dir = repository / git_dir
    sparse_file = git_dir / "info" / "sparse-checkout"
    try:
        sparse_file.parent.mkdir(parents=True, exist_ok=True)
        sparse_file.write_text("\n".join(plan.patterns) + "\n", encoding="utf-8")
    except OSError as error:
        return CommandResult(127, "", f"{type(error).__name__}: {error}")
    return CommandResult(0, str(sparse_file), "")


@dataclass(frozen=True, slots=True)
class FixedCommitMaterialization:
    ok: bool
    mode: str
    failure_step: str | None = None
    detail: str | None = None
    checkout_error: str | None = None


def _mark_auto_excluded_skip_worktree(
    repository: Path, auto_excluded_paths: Sequence[str]
) -> CommandResult:
    if not auto_excluded_paths:
        return CommandResult(0, "", "")
    skip_input = "\0".join(auto_excluded_paths) + "\0"
    bulk = _git_output(
        repository,
        "-c",
        "core.protectNTFS=false",
        "update-index",
        "--skip-worktree",
        "-z",
        "--stdin",
        input_text=skip_input,
    )
    if bulk.returncode != 0:
        return bulk
    # Git for Windows can case-fold a stdin pathspec even with
    # core.ignoreCase=false. Re-apply each literal pathspec so case-collision
    # losers receive their own skip-worktree bit. Invalid NTFS spellings may
    # reject this second form; the successful index-only bulk update above is
    # authoritative for those entries.
    for path in auto_excluded_paths:
        _git_output(
            repository,
            "-c",
            "core.protectNTFS=false",
            "update-index",
            "--skip-worktree",
            "--",
            f":(top,literal){path}",
        )
    return bulk


def _materialize_fixed_commit(
    repository: Path,
    *,
    commit: str,
    auto_excluded_paths: Sequence[str],
    safe_checkout_paths: Sequence[str],
) -> FixedCommitMaterialization:
    checkout = _git_output(repository, "checkout", "--detach", commit, timeout=1800)
    if checkout.returncode == 0:
        marked = _mark_auto_excluded_skip_worktree(repository, auto_excluded_paths)
        if marked.returncode != 0:
            return FixedCommitMaterialization(
                ok=False,
                mode="checkout",
                failure_step="mark_auto_excluded_skip_worktree",
                detail=marked.stderr[-1000:],
            )
        reapplied = _git_output(repository, "sparse-checkout", "reapply", timeout=1800)
        if reapplied.returncode == 0:
            marked_after_reapply = _mark_auto_excluded_skip_worktree(
                repository, auto_excluded_paths
            )
            if marked_after_reapply.returncode == 0:
                return FixedCommitMaterialization(ok=True, mode="checkout")
            return FixedCommitMaterialization(
                ok=False,
                mode="checkout",
                failure_step="remark_auto_excluded_skip_worktree",
                detail=marked_after_reapply.stderr[-1000:],
            )
        return FixedCommitMaterialization(
            ok=False,
            mode="checkout",
            failure_step="sparse_checkout_reapply",
            detail=reapplied.stderr[-1000:],
        )

    checkout_error = checkout.stderr[-1000:]
    fallback_steps = (
        (
            "reset_mixed",
            ("-c", "core.protectNTFS=false", "reset", "--mixed", "--quiet"),
            None,
        ),
        ("detach_head", ("update-ref", "--no-deref", "HEAD", commit), None),
        (
            "read_fixed_tree",
            ("-c", "core.protectNTFS=false", "read-tree", "--reset", commit),
            None,
        ),
    )
    for step, arguments, input_text in fallback_steps:
        result = _git_output(repository, *arguments, input_text=input_text)
        if result.returncode != 0:
            return FixedCommitMaterialization(
                ok=False,
                mode="index-head-fallback",
                failure_step=step,
                detail=result.stderr[-1000:],
                checkout_error=checkout_error,
            )
    marked = _mark_auto_excluded_skip_worktree(repository, auto_excluded_paths)
    if marked.returncode != 0:
        return FixedCommitMaterialization(
            ok=False,
            mode="index-head-fallback",
            failure_step="mark_auto_excluded_skip_worktree",
            detail=marked.stderr[-1000:],
            checkout_error=checkout_error,
        )
    reapplied = _git_output(repository, "sparse-checkout", "reapply", timeout=1800)
    if reapplied.returncode != 0:
        return FixedCommitMaterialization(
            ok=False,
            mode="index-head-fallback",
            failure_step="sparse_checkout_reapply",
            detail=reapplied.stderr[-1000:],
            checkout_error=checkout_error,
        )
    marked_after_reapply = _mark_auto_excluded_skip_worktree(repository, auto_excluded_paths)
    if marked_after_reapply.returncode != 0:
        return FixedCommitMaterialization(
            ok=False,
            mode="index-head-fallback",
            failure_step="remark_auto_excluded_skip_worktree",
            detail=marked_after_reapply.stderr[-1000:],
            checkout_error=checkout_error,
        )
    if safe_checkout_paths:
        checkout_input = "\0".join(safe_checkout_paths) + "\0"
        materialized = _git_output(
            repository,
            "checkout-index",
            "-f",
            "-z",
            "--stdin",
            timeout=1800,
            input_text=checkout_input,
        )
        if materialized.returncode != 0:
            return FixedCommitMaterialization(
                ok=False,
                mode="index-head-fallback",
                failure_step="materialize_safe_index_paths",
                detail=materialized.stderr[-1000:],
                checkout_error=checkout_error,
            )
    return FixedCommitMaterialization(
        ok=True,
        mode="index-head-fallback",
        checkout_error=checkout_error,
    )


def _verify_auto_exclusions(
    repository: Path,
    auto_excluded_paths: Sequence[str],
    *,
    allow_index_only_deletions: bool = False,
    filesystem_alias_winners: Mapping[str, str] | None = None,
) -> tuple[list[str], str | None]:
    if not auto_excluded_paths:
        return [], None
    tracked = _git_output(repository, "ls-files", "-t", "-z")
    if tracked.returncode != 0:
        return list(auto_excluded_paths), "git_ls_files_failed"
    states = {}
    entries = tracked.stdout.split("\0") if "\0" in tracked.stdout else tracked.stdout.splitlines()
    for entry in entries:
        if len(entry) >= 3 and entry[1] == " ":
            states[entry[2:]] = entry[0]
    failures = []
    alias_winners = dict(filesystem_alias_winners or {})
    aliases = set(alias_winners)
    index_blobs: dict[str, str] = {}
    if aliases:
        staged = _git_output(repository, "ls-files", "-s", "-z")
        if staged.returncode != 0:
            return list(auto_excluded_paths), "git_ls_files_stage_failed"
        staged_entries = (
            staged.stdout.split("\0") if "\0" in staged.stdout else staged.stdout.splitlines()
        )
        for entry in staged_entries:
            if "\t" not in entry:
                continue
            metadata, path = entry.split("\t", 1)
            fields = metadata.split()
            if len(fields) >= 2:
                index_blobs[path] = fields[1]
    for path in auto_excluded_paths:
        state = states.get(path)
        if path in aliases:
            winner = alias_winners[path]
            winner_path = repository.joinpath(*winner.split("/"))
            try:
                actual_names = os.listdir(winner_path.parent)
            except OSError:
                failures.append(path)
                continue
            if winner_path.name not in actual_names or path.split("/")[-1] in actual_names:
                failures.append(path)
                continue
            physical_blob = _git_output(repository, "hash-object", "--no-filters", "--", winner)
            if physical_blob.returncode != 0 or physical_blob.stdout != index_blobs.get(winner):
                failures.append(path)
                continue
        else:
            filesystem_path = repository.joinpath(*path.split("/"))
            try:
                if os.path.lexists(filesystem_path):
                    failures.append(path)
                    continue
            except OSError:
                failures.append(path)
                continue
        if state != "S" and not (state == "H" and (allow_index_only_deletions or path in aliases)):
            failures.append(path)
    return sorted(set(failures)), None


def _status_entries(output: str) -> list[str]:
    return [
        entry for entry in (output.split("\0") if "\0" in output else output.splitlines()) if entry
    ]


def _partition_dirty_entries(
    output: str, *, auto_excluded_paths: Sequence[str]
) -> tuple[list[str], list[str]]:
    allowed_paths = set(auto_excluded_paths)
    allowed = []
    disallowed = []
    for entry in _status_entries(output):
        if entry.startswith(" D ") and entry[3:] in allowed_paths:
            allowed.append(entry)
        else:
            disallowed.append(entry)
    return allowed, disallowed


def _post_clone_verification_failure(
    approval: RepositoryApproval,
    *,
    head: CommandResult,
    dirty: CommandResult,
    remote: CommandResult,
    auto_excluded_paths: Sequence[str],
) -> dict[str, Any] | None:
    remote_link = canonical_link(remote.stdout)
    actual_head = head.stdout.casefold() if head.returncode == 0 else None
    actual_remote = remote_link.url if remote_link is not None else None
    allowed_deletions, disallowed_dirty = _partition_dirty_entries(
        dirty.stdout,
        auto_excluded_paths=auto_excluded_paths,
    )
    differences = []
    if actual_head != approval.commit:
        differences.append("head_mismatch")
    if dirty.returncode != 0:
        differences.append("dirty_state_unreadable")
    elif disallowed_dirty:
        differences.append("working_tree_dirty")
    if remote.returncode != 0:
        differences.append("remote_unreadable")
    elif remote_link is None or remote_link.key != approval.link.key:
        differences.append("remote_mismatch")
    if not differences:
        return None
    return {
        "status": "failed",
        "reason": "post_clone_verification_failed",
        "differences": differences,
        "verification": {
            "expected_head": approval.commit,
            "actual_head": actual_head,
            "head_command_returncode": head.returncode,
            "dirty_command_returncode": dirty.returncode,
            "dirty_entries": disallowed_dirty[:100],
            "allowed_excluded_deletions": allowed_deletions[:100],
            "expected_remote": approval.link.url,
            "actual_remote": actual_remote,
            "remote_command_returncode": remote.returncode,
        },
        "auto_excluded_paths": list(auto_excluded_paths),
    }


def build_clone_plan(
    approvals: ApprovalSet,
    *,
    repository_index: Mapping[str, list[Path]],
    archive_root: Path | None,
) -> list[dict[str, Any]]:
    plan = []
    for key, approval in sorted(approvals.repositories.items()):
        existing = sorted(str(path.resolve()) for path in repository_index.get(key, []))
        destination = (
            str((archive_root / approval.destination).resolve(strict=False))
            if archive_root is not None
            else None
        )
        status = "reuse_existing" if existing else "approved_not_applied"
        if destination is not None and Path(destination).exists() and not existing:
            status = "blocked_destination_exists"
        plan.append(
            {
                "canonical_key": key,
                "url": approval.link.url,
                "clone_url": approval.link.clone_url,
                "commit": approval.commit,
                "destination": destination,
                "sparse_paths": list(approval.sparse_paths),
                "sparse_checkout_mode": "non-cone",
                "auto_exclusions_computed_from_fixed_tree": True,
                "paper_ids": [item.paper_id for item in approval.papers],
                "status": status,
                "existing_repositories": existing,
                "environment": {
                    "GIT_LFS_SKIP_SMUDGE": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "Never",
                },
                "commands": [
                    [
                        "git",
                        "clone",
                        "--filter=blob:none",
                        "--no-checkout",
                        "--no-recurse-submodules",
                        approval.link.clone_url,
                        "${STAGING}",
                    ],
                    [
                        "git",
                        "-C",
                        "${STAGING}",
                        "fetch",
                        "--depth=1",
                        "--filter=blob:none",
                        "origin",
                        approval.commit,
                    ],
                    ["git", "-C", "${STAGING}", "checkout", "--detach", approval.commit],
                ],
            }
        )
    return plan


def _execute_clone(approval: RepositoryApproval, *, archive_root: Path) -> dict[str, Any]:
    if not archive_root.exists() or not archive_root.is_dir() or _is_reparse_point(archive_root):
        return {"status": "failed", "reason": "archive_root_missing_or_reparse_point"}
    destination = (archive_root / approval.destination).resolve(strict=False)
    if not _within(destination, archive_root):
        return {"status": "failed", "reason": "destination_outside_archive_root"}
    if destination.exists():
        return {"status": "failed", "reason": "destination_exists", "path": str(destination)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".paper-code-audit-", dir=archive_root) as temporary:
        staging = Path(temporary) / "repository"
        commands = [
            (
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--no-recurse-submodules",
                approval.link.clone_url or "",
                str(staging),
            ),
            (
                "git",
                "-C",
                str(staging),
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                "origin",
                approval.commit,
            ),
        ]
        for command in commands:
            result = _run_command(command, timeout=1800)
            if result.returncode != 0:
                return {
                    "status": "failed",
                    "reason": "git_clone_or_fetch_failed",
                    "detail": result.stderr[-1000:],
                }
        tree = _git_output(staging, "ls-tree", "-r", "-z", "--name-only", approval.commit)
        if tree.returncode != 0:
            return {
                "status": "failed",
                "reason": "git_tree_unreadable",
                "detail": tree.stderr[-1000:],
            }
        try:
            tree_paths = _parse_tree_paths(tree.stdout)
            sparse_plan = _build_sparse_checkout_plan(
                tree_paths,
                explicit_sparse_paths=approval.sparse_paths,
            )
        except ValueError as error:
            return {
                "status": "failed",
                "reason": "sparse_checkout_plan_failed",
                "detail": str(error)[:1000],
            }
        sparse_configured = _configure_non_cone_sparse_checkout(staging, sparse_plan)
        if sparse_configured.returncode != 0:
            return {
                "status": "failed",
                "reason": "sparse_checkout_configuration_failed",
                "detail": sparse_configured.stderr[-1000:],
                "auto_excluded_paths": list(sparse_plan.auto_excluded_paths),
            }
        materialization = _materialize_fixed_commit(
            staging,
            commit=approval.commit,
            auto_excluded_paths=sparse_plan.auto_excluded_paths,
            safe_checkout_paths=sparse_plan.checkout_paths,
        )
        if not materialization.ok:
            return {
                "status": "failed",
                "reason": "fixed_commit_materialization_failed",
                "failure_step": materialization.failure_step,
                "materialization_mode": materialization.mode,
                "detail": materialization.detail,
                "checkout_error": materialization.checkout_error,
                "auto_excluded_paths": list(sparse_plan.auto_excluded_paths),
            }
        materialized_unsafe, exclusion_check_error = _verify_auto_exclusions(
            staging,
            sparse_plan.auto_excluded_paths,
            allow_index_only_deletions=materialization.mode == "index-head-fallback",
            filesystem_alias_winners=dict(sparse_plan.casefold_collision_winners),
        )
        if materialized_unsafe or exclusion_check_error:
            return {
                "status": "failed",
                "reason": "auto_excluded_paths_materialized",
                "materialized_or_unverified_paths": materialized_unsafe,
                "verification_error": exclusion_check_error,
                "auto_excluded_paths": list(sparse_plan.auto_excluded_paths),
            }
        head = _git_output(staging, "rev-parse", "HEAD")
        dirty = _git_output(
            staging,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
        )
        remote = _git_output(staging, "remote", "get-url", "origin")
        verification_failure = _post_clone_verification_failure(
            approval,
            head=head,
            dirty=dirty,
            remote=remote,
            auto_excluded_paths=sparse_plan.auto_excluded_paths,
        )
        if verification_failure is not None:
            return verification_failure
        allowed_excluded_deletions, _ = _partition_dirty_entries(
            dirty.stdout,
            auto_excluded_paths=sparse_plan.auto_excluded_paths,
        )
        if destination.exists():
            return {"status": "failed", "reason": "destination_appeared_during_clone"}
        staging.replace(destination)
    return {
        "status": "archived",
        "path": str(destination),
        "commit": approval.commit,
        "remote": approval.link.url,
        "lfs_smudge_skipped": True,
        "submodules_initialized": False,
        "sparse_paths": list(approval.sparse_paths),
        "sparse_checkout_mode": "non-cone",
        "materialization_mode": materialization.mode,
        "initial_checkout_error": materialization.checkout_error,
        "sparse_patterns_count": len(sparse_plan.patterns),
        "selected_tree_paths": sparse_plan.selected_path_count,
        "auto_excluded_paths": list(sparse_plan.auto_excluded_paths),
        "casefold_collision_paths": list(sparse_plan.casefold_collision_paths),
        "casefold_collision_winners": dict(sparse_plan.casefold_collision_winners),
        "auto_exclusion_reasons": {
            path: list(reasons) for path, reasons in sparse_plan.auto_exclusion_reasons
        },
        "allowed_excluded_deletions": allowed_excluded_deletions,
    }


def apply_approved_clones(
    approvals: ApprovalSet,
    *,
    archive_root: Path,
    repository_index: Mapping[str, list[Path]],
    clone_jobs: int = 1,
) -> list[dict[str, Any]]:
    if not 1 <= clone_jobs <= 8:
        raise ValueError("clone_jobs must be between 1 and 8")
    results: list[dict[str, Any]] = []
    missing: list[tuple[str, RepositoryApproval]] = []
    for key, approval in sorted(approvals.repositories.items()):
        if repository_index.get(key):
            results.append(
                {
                    "canonical_key": key,
                    "status": "reused_existing",
                    "paths": sorted(str(path.resolve()) for path in repository_index[key]),
                }
            )
            continue
        missing.append((key, approval))

    destinations: dict[str, list[tuple[str, RepositoryApproval]]] = defaultdict(list)
    for key, approval in missing:
        destination_key = approval.destination.replace("/", os.sep).casefold()
        destinations[destination_key].append((key, approval))
    runnable = []
    for _destination_key, candidates in sorted(destinations.items()):
        if len(candidates) == 1:
            runnable.append(candidates[0])
            continue
        canonical_keys = sorted(key for key, _ in candidates)
        for key, approval in candidates:
            results.append(
                {
                    "canonical_key": key,
                    "status": "failed",
                    "reason": "duplicate_destination",
                    "destination": approval.destination,
                    "conflicting_canonical_keys": canonical_keys,
                }
            )

    if runnable:
        worker_count = min(clone_jobs, len(runnable))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="paper-code-clone",
        ) as executor:
            futures = {
                executor.submit(_execute_clone, approval, archive_root=archive_root): key
                for key, approval in runnable
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    result = future.result()
                except Exception as error:  # isolate an unexpected worker failure
                    result = {
                        "status": "failed",
                        "reason": "clone_worker_exception",
                        "detail": f"{type(error).__name__}: {error}"[:1000],
                    }
                results.append({"canonical_key": key, **result})
    return sorted(results, key=lambda item: str(item["canonical_key"]))


def run_audit(
    *,
    metadata_paths: Sequence[Path],
    paper_record_paths: Sequence[Path],
    core_root: Path,
    repository_roots: Sequence[Path] = (),
    approvals_path: Path | None = None,
    archive_root: Path | None = None,
    apply: bool = False,
    clone_jobs: int = 1,
) -> dict[str, Any]:
    if not 1 <= clone_jobs <= 8:
        raise ValueError("clone_jobs must be between 1 and 8")
    core_root = core_root.expanduser().resolve(strict=True)
    if not core_root.is_dir():
        raise ValueError(f"core root is not a directory: {core_root}")
    papers = load_paper_records(paper_record_paths, core_root=core_root)
    papers = attach_metadata(papers, load_metadata(metadata_paths))
    approvals = load_approvals(approvals_path, papers)
    roots = list(repository_roots) or [core_root]
    repositories, repository_warnings = scan_repositories(roots)
    repository_index = _repository_index(repositories)
    clone_plan = build_clone_plan(
        approvals,
        repository_index=repository_index,
        archive_root=archive_root,
    )
    apply_results: list[dict[str, Any]] = []
    if apply:
        if approvals_path is None or archive_root is None:
            raise ValueError("--apply requires --approvals and --archive-root")
        apply_results = apply_approved_clones(
            approvals,
            archive_root=archive_root,
            repository_index=repository_index,
            clone_jobs=clone_jobs,
        )
        if any(item["status"] == "archived" for item in apply_results):
            repositories, repository_warnings = scan_repositories([*roots, archive_root])
            repository_index = _repository_index(repositories)

    inspection_cache: dict[str, dict[str, Any]] = {}
    paper_results = []
    network_queue = []
    for paper in sorted(papers, key=lambda item: (item.paper_id.casefold(), item.title.casefold())):
        result, queue = _paper_audit(
            paper,
            repository_index=repository_index,
            approvals=approvals,
            inspection_cache=inspection_cache,
            core_root=core_root,
        )
        paper_results.append(result)
        network_queue.extend(queue)
    deduped_queue = {
        (item["paper_id"], item["action"], item.get("url")): item for item in network_queue
    }
    network_queue = sorted(
        deduped_queue.values(),
        key=lambda item: (
            item["priority"],
            item["paper_id"],
            item["action"],
            item.get("url") or "",
        ),
    )
    status_counts = Counter(item["status"] for item in paper_results)
    blocker_count = sum(bool(item["hard_block"]) for item in paper_results)
    reuse: dict[str, set[str]] = defaultdict(set)
    for item in paper_results:
        for url in item["official_urls"]:
            link = canonical_link(url)
            if link:
                reuse[link.key].add(item["paper_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "mode": "apply" if apply else "offline_audit",
        "network_used": apply,
        "inputs": {
            "metadata": [str(path.resolve()) for path in metadata_paths],
            "paper_records": [str(path.resolve()) for path in paper_record_paths],
            "core_root": str(core_root),
            "repository_roots": [str(path.resolve()) for path in roots],
            "approvals": str(approvals_path.resolve()) if approvals_path else None,
            "archive_root": str(archive_root.resolve()) if archive_root else None,
            "clone_jobs": clone_jobs,
        },
        "summary": {
            "papers": len(paper_results),
            "status_counts": dict(sorted(status_counts.items())),
            "hard_blockers": blocker_count,
            "network_queue": len(network_queue),
            "repositories_scanned": len(repositories),
            "approved_repositories": len(approvals.repositories),
            "approved_no_code": len(approvals.no_code),
        },
        "repository_scan_warnings": sorted(set(repository_warnings)),
        "repository_reuse": [
            {"canonical_key": key, "paper_ids": sorted(paper_ids)}
            for key, paper_ids in sorted(reuse.items())
            if len(paper_ids) > 1
        ],
        "repositories": repositories,
        "clone_plan": clone_plan,
        "apply_results": apply_results,
        "papers": paper_results,
        "network_queue": network_queue,
    }


def _output_paths(prefix: Path) -> tuple[Path, Path, Path, Path]:
    base = (
        prefix.with_suffix("")
        if prefix.suffix.casefold() in {".json", ".csv", ".jsonl"}
        else prefix
    )
    return (
        Path(f"{base}.json"),
        Path(f"{base}.csv"),
        Path(f"{base}.network-queue.jsonl"),
        Path(f"{base}.clone-plan.json"),
    )


def write_reports(prefix: Path, report: Mapping[str, Any]) -> tuple[Path, Path, Path, Path]:
    json_path, csv_path, queue_path, plan_path = _output_paths(prefix)
    for path in (json_path, csv_path, queue_path, plan_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = (
            "paper_id",
            "title",
            "year",
            "domains",
            "status",
            "hard_block",
            "official_urls",
            "local_paths",
            "candidate_urls",
            "api_status",
            "allowlist_exact_title_candidate",
            "issues",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for paper in report["papers"]:
            writer.writerow(
                {
                    "paper_id": paper["paper_id"],
                    "title": paper["title"],
                    "year": paper["year"],
                    "domains": json.dumps(paper["domains"], ensure_ascii=False),
                    "status": paper["status"],
                    "hard_block": paper["hard_block"],
                    "official_urls": json.dumps(paper["official_urls"], ensure_ascii=False),
                    "local_paths": json.dumps(
                        [item["path"] for item in paper["local_matches"]], ensure_ascii=False
                    ),
                    "candidate_urls": json.dumps(
                        [item["url"] for item in paper["candidate_leads"]], ensure_ascii=False
                    ),
                    "api_status": paper["html_api_mapping"]["api_code_action_expected"],
                    "allowlist_exact_title_candidate": paper["html_api_mapping"][
                        "allowlist_exact_title_candidate"
                    ],
                    "issues": json.dumps(paper["issues"], ensure_ascii=False),
                }
            )
    with queue_path.open("w", encoding="utf-8") as handle:
        for item in report["network_queue"]:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    plan_path.write_text(
        json.dumps(report["clone_plan"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return json_path, csv_path, queue_path, plan_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, action="append", default=[])
    parser.add_argument("--paper-records", type=Path, action="append", default=[])
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, action="append", default=[])
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--clone-jobs",
        type=int,
        choices=range(1, 9),
        default=1,
        metavar="N",
        help="parallel approved repository clones (1..8, default: 1)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return 3 while any paper lacks official_local or reviewed not_provided closure",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.metadata and not arguments.paper_records:
        print("at least one --metadata or --paper-records input is required")
        return 2
    try:
        report = run_audit(
            metadata_paths=arguments.metadata,
            paper_record_paths=arguments.paper_records,
            core_root=arguments.core_root,
            repository_roots=arguments.repository_root,
            approvals_path=arguments.approvals,
            archive_root=arguments.archive_root,
            apply=arguments.apply,
            clone_jobs=arguments.clone_jobs,
        )
        paths = write_reports(arguments.output_prefix, report)
    except (OSError, ValueError) as error:
        print(f"paper-code-audit failed: {error}")
        return 2
    print(
        json.dumps(
            {
                "summary": report["summary"],
                "reports": [str(path.resolve()) for path in paths],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if any(item.get("status") == "failed" for item in report["apply_results"]):
        return 3
    if arguments.strict and report["summary"]["hard_blockers"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
