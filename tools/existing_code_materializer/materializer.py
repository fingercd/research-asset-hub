"""Normalize reviewed code evidence and emit a no-network materialization plan.

The planner is deliberately unable to clone repositories or edit Hub configuration.
It emits the exact approval document consumed by :mod:`tools.paper_code_audit`,
immutable generic-manifest drafts, and a reviewable no-code allowlist delta.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml

REPORT_SCHEMA = "existing-code-materializer-report/v1"
PLAN_SCHEMA = "existing-code-materializer-plan/v1"
APPROVAL_SCHEMA_VERSION = "paper-code-approvals/v1"
DEFAULT_BATCH_ID = "20260831"
ARCHIVE_SUBPATH = Path("共享代码") / "2026-08-31-存量领域补录" / "upstream"
ALLOWED_STATUSES = frozenset(
    {"official_public", "announced_pending", "none_found", "third_party_only"}
)
NON_PUBLIC_STATUSES = ALLOWED_STATUSES - {"official_public"}
SAFE_BATCH_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
DATE_PREFIX_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-")
FAILURE_COUNT_RE = re.compile(r"^❌\s+(?P<count>\d+)\s+处不匹配：\s*$")
LICENSE_RE = re.compile(r"^[^\x00-\x1f<>]{1,127}$")
MANIFEST_LICENSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .()+:/_-]{0,126}$")
SOURCE_RECORD_SAFE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
GITLAB_REPOSITORY_HOSTS = frozenset({"gitlab.com", "gitlab.mpi-sws.org"})
SENSITIVE_QUERY_KEY_RE = re.compile(
    r"(?:^|[_-])(?:access|api|auth|credential|key|password|secret|signature|token)(?:$|[_-])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AuditSpec:
    stem: str
    domain: str

    @property
    def audit_name(self) -> str:
        return f"{self.stem}.jsonl"

    @property
    def unit_action_name(self) -> str:
        return f"2026-08-31-unit-actions-{self.domain}.txt"


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    key: str
    url: str
    clone_url: str
    destination: str
    tree_ref: str | None
    sparse_path: str | None


AUDIT_SPECS = (
    AuditSpec("video", "video-llm-compression"),
    AuditSpec("agent", "agent"),
    AuditSpec("fine-tuning", "fine-tuning"),
    AuditSpec("generation", "generation"),
    AuditSpec("infra", "infra"),
    AuditSpec("world-model", "world-model"),
    AuditSpec("vision-language-navigation", "vision-language-navigation"),
)


class MaterializerError(ValueError):
    """A validation failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class AuditRow:
    domain: str
    exact_title: str
    status: str
    official_url: str | None
    commit: str | None
    license: str | None
    evidence_urls: tuple[str, ...]
    evidence_reason: str
    reviewed_at: str
    source_file: str
    source_line: int
    detailed_evidence: tuple[dict[str, Any], ...]
    target: RepositoryTarget | None


@dataclass(frozen=True, slots=True)
class CatalogPaper:
    asset_id: str
    domain: str
    title: str
    title_zh: str | None
    authors: tuple[str, ...]
    year: int | None
    venue: str | None
    source_urls: tuple[str, ...]
    identifiers: tuple[dict[str, Any], ...]
    doi: str | None
    arxiv_id: str | None


@dataclass(frozen=True, slots=True)
class BoundRow:
    audit: AuditRow
    paper: CatalogPaper


@dataclass(slots=True)
class RepositoryGroup:
    target: RepositoryTarget
    commit: str
    license: str
    rows: list[BoundRow] = field(default_factory=list)
    sparse_paths: set[str] = field(default_factory=set)
    requires_full_checkout: bool = False


@dataclass(frozen=True, slots=True)
class BuildResult:
    report: dict[str, Any]
    approvals: dict[str, Any]
    paper_records: tuple[dict[str, Any], ...]
    manifests: Mapping[str, dict[str, Any]]
    source_plan: tuple[dict[str, Any], ...]
    allowlist_additions: Mapping[str, tuple[str, ...]]
    no_code_evidence: tuple[dict[str, Any], ...]


def _fail(code: str, message: str, **details: Any) -> NoReturn:
    raise MaterializerError(code, message, **details)


def _clean_text(value: Any, *, label: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            _fail("missing_text", f"{label} is required", field=label)
        return None
    if not isinstance(value, str):
        _fail("invalid_text", f"{label} must be a string", field=label)
    text = value.strip()
    if not text:
        if required:
            _fail("missing_text", f"{label} is required", field=label)
        return None
    if "\x00" in text or any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        _fail("invalid_text", f"{label} contains invalid Unicode", field=label)
    return text


def _aliased_text(
    raw: Mapping[str, Any],
    keys: Sequence[str],
    *,
    label: str,
    required: bool = False,
) -> str | None:
    found: list[tuple[str, str]] = []
    for key in keys:
        value = _clean_text(raw.get(key), label=f"{label}.{key}")
        if value is not None:
            found.append((key, value))
    distinct = {value for _, value in found}
    if len(distinct) > 1:
        _fail(
            "conflicting_aliases",
            f"{label} declares conflicting aliases",
            aliases={key: value for key, value in found},
        )
    if not found:
        if required:
            _fail("missing_alias", f"{label} is required", aliases=list(keys))
        return None
    return found[0][1]


def _safe_license(value: Any, *, required: bool) -> str | None:
    text = _clean_text(value, label="license", required=required)
    if text is not None and not LICENSE_RE.fullmatch(text):
        _fail("invalid_license", "license contains unsafe characters or is too long")
    if text is None:
        return None
    normalized = re.sub(r"\s+\([^\r\n]*\)$", "", text).strip()
    if not MANIFEST_LICENSE_RE.fullmatch(normalized):
        _fail("invalid_license", "license cannot be normalized to a safe manifest value")
    return normalized


def _safe_evidence_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ValueError("evidence URL is malformed") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("evidence URL must be HTTP(S)")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("evidence URL contains userinfo or port")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if SENSITIVE_QUERY_KEY_RE.search(key):
            raise ValueError("evidence URL contains a sensitive query key")
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.hostname.casefold(), parsed.path, parsed.query, "")
    )


def _safe_sparse_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe repository sparse path: {value}")
    if not all(SAFE_SEGMENT_RE.fullmatch(part) for part in path.parts):
        raise ValueError(f"unsafe repository sparse path: {value}")
    return path.as_posix()


def _repository_target(value: str) -> RepositoryTarget:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"malformed official repository URL: {value}") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
        or "%" in parsed.path
    ):
        raise ValueError(f"official repository URL must be clean HTTPS: {value}")
    host = (parsed.hostname or "").casefold().rstrip(".")
    segments = [part for part in parsed.path.split("/") if part]
    tree_ref: str | None = None
    sparse_path: str | None = None
    namespace: list[str]
    destination_host: str
    if host == "github.com":
        if len(segments) < 2:
            raise ValueError(f"official URL is not a GitHub repository: {value}")
        owner = segments[0]
        repository = segments[1].removesuffix(".git")
        if not SAFE_SEGMENT_RE.fullmatch(owner) or not SAFE_SEGMENT_RE.fullmatch(repository):
            raise ValueError(f"official URL has unsafe repository segments: {value}")
        if len(segments) > 2:
            if len(segments) < 4 or segments[2].casefold() != "tree":
                raise ValueError(f"unsupported GitHub official URL subpath: {value}")
            tree_ref = segments[3]
            if not SAFE_SEGMENT_RE.fullmatch(tree_ref):
                raise ValueError(f"unsafe GitHub tree ref: {value}")
            if len(segments) > 4:
                sparse_path = _safe_sparse_path("/".join(segments[4:]))
        namespace = [owner, repository]
        destination_host = "github"
    elif host in GITLAB_REPOSITORY_HOSTS:
        if "-" in segments:
            marker = segments.index("-")
            namespace = segments[:marker]
            tail = segments[marker + 1 :]
            if len(tail) < 2 or tail[0].casefold() != "tree":
                raise ValueError(f"unsupported GitLab official URL subpath: {value}")
            tree_ref = tail[1]
            if not SAFE_SEGMENT_RE.fullmatch(tree_ref):
                raise ValueError(f"unsafe GitLab tree ref: {value}")
            if len(tail) > 2:
                sparse_path = _safe_sparse_path("/".join(tail[2:]))
        else:
            namespace = segments
        if len(namespace) < 2:
            raise ValueError(f"official URL is not a GitLab repository: {value}")
        namespace[-1] = namespace[-1].removesuffix(".git")
        if not all(SAFE_SEGMENT_RE.fullmatch(segment) for segment in namespace):
            raise ValueError(f"official URL has unsafe repository segments: {value}")
        destination_host = "gitlab" if host == "gitlab.com" else host
    else:
        raise ValueError(f"official URL must use github.com or gitlab.com: {value}")
    canonical_url = f"https://{host}/{'/'.join(namespace)}"
    key = f"{host}/{'/'.join(namespace)}".casefold()
    destination = "/".join((destination_host, *namespace))
    return RepositoryTarget(
        key=key,
        url=canonical_url,
        clone_url=f"{canonical_url}.git",
        destination=destination,
        tree_ref=tree_ref,
        sparse_path=sparse_path,
    )


def _evidence_entries(raw: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    details: list[dict[str, Any]] = []
    urls: list[str] = []

    def add_url(value: Any, *, field_name: str) -> None:
        text = _clean_text(value, label=field_name)
        if text is None:
            return
        try:
            safe = _safe_evidence_url(text)
        except ValueError as error:
            _fail("unsafe_evidence_url", str(error), field=field_name)
        urls.append(safe)

    raw_urls = raw.get("evidence_urls")
    if raw_urls is not None:
        if not isinstance(raw_urls, list):
            _fail("invalid_evidence", "evidence_urls must be a list")
        for index, value in enumerate(raw_urls):
            add_url(value, field_name=f"evidence_urls[{index}]")

    for field_name in (
        "officiality_evidence",
        "search_evidence",
        "companion_repositories",
        "third_party_candidates",
        "third_party_repositories",
        "third_party_urls",
    ):
        values = raw.get(field_name)
        if values is None:
            continue
        if not isinstance(values, list):
            _fail("invalid_evidence", f"{field_name} must be a list")
        for index, value in enumerate(values):
            if isinstance(value, str):
                text = _clean_text(value, label=f"{field_name}[{index}]", required=True)
                assert text is not None
                if text.startswith(("http://", "https://")):
                    add_url(text, field_name=f"{field_name}[{index}]")
                    details.append({"kind": field_name, "url": text})
                else:
                    details.append({"kind": field_name, "text": text})
                continue
            if not isinstance(value, Mapping):
                _fail("invalid_evidence", f"{field_name}[{index}] must be text or an object")
            normalized: dict[str, Any] = {"kind": field_name}
            for key in ("url", "finding", "query", "backend", "text", "note"):
                text = _clean_text(value.get(key), label=f"{field_name}[{index}].{key}")
                if text is not None:
                    normalized[key] = text
            if "url" in normalized:
                add_url(normalized["url"], field_name=f"{field_name}[{index}].url")
            if len(normalized) == 1:
                _fail("invalid_evidence", f"{field_name}[{index}] has no supported evidence")
            details.append(normalized)
    return details, urls


def _parse_audit_row(
    raw: Mapping[str, Any],
    *,
    spec: AuditSpec,
    source_path: Path,
    line_number: int,
    fallback_reviewed_at: str,
) -> AuditRow:
    title = _aliased_text(raw, ("exact_title", "title"), label="paper title", required=True)
    assert title is not None
    status_text = _clean_text(raw.get("status"), label="status", required=True)
    assert status_text is not None
    status = status_text.casefold()
    if status not in ALLOWED_STATUSES:
        _fail(
            "invalid_status",
            f"unsupported status at {source_path}:{line_number}: {status}",
            status=status,
        )
    official_url = _aliased_text(
        raw,
        ("canonical_repo_url", "official_repo_url", "repo_url", "official_url"),
        label="official repository URL",
    )
    commit = _aliased_text(
        raw,
        ("head", "pinned_head", "head_commit", "commit"),
        label="repository commit",
    )
    if commit is not None:
        commit = commit.casefold()
    raw_license = _clean_text(raw.get("license"), label="license")
    license_value = _safe_license(raw.get("license"), required=status == "official_public")
    target: RepositoryTarget | None = None
    if status == "official_public":
        if official_url is None or commit is None:
            _fail(
                "missing_official_repository",
                f"official_public row needs URL and commit at {source_path}:{line_number}",
            )
        if not COMMIT_RE.fullmatch(commit):
            _fail(
                "invalid_commit",
                f"official_public commit must be 40 hex at {source_path}:{line_number}",
            )
        try:
            target = _repository_target(official_url)
        except ValueError as error:
            _fail("unsafe_repository_url", str(error), url=official_url)
    elif official_url is not None or commit is not None:
        _fail(
            "unexpected_repository",
            f"non-public row declares repository URL/commit at {source_path}:{line_number}",
        )

    detailed_evidence, evidence_urls = _evidence_entries(raw)
    landing_url = _clean_text(raw.get("landing_url"), label="landing_url")
    if landing_url is not None:
        try:
            evidence_urls.append(_safe_evidence_url(landing_url))
        except ValueError as error:
            _fail("unsafe_evidence_url", str(error), field="landing_url")
    if official_url is not None:
        try:
            evidence_urls.append(_safe_evidence_url(official_url))
        except ValueError as error:
            _fail("unsafe_repository_url", str(error), url=official_url)
    reason_parts = []
    for key in ("evidence_reason", "notes", "release_scope", "audit_scope"):
        value = _clean_text(raw.get(key), label=key)
        if value is not None:
            reason_parts.append(f"{key}: {value}")
    if raw_license is not None and raw_license != license_value:
        reason_parts.append(f"license_evidence: {raw_license}")
    if not reason_parts:
        reason_parts.extend(
            str(item.get("finding") or item.get("text") or item.get("note"))
            for item in detailed_evidence
            if item.get("finding") or item.get("text") or item.get("note")
        )
    if not reason_parts:
        _fail(
            "missing_evidence_reason",
            f"audit row has no reasoning at {source_path}:{line_number}",
        )
    reviewed_at = (
        _aliased_text(raw, ("reviewed_at", "audit_date"), label="reviewed_at")
        or fallback_reviewed_at
    )
    return AuditRow(
        domain=spec.domain,
        exact_title=title,
        status=status,
        official_url=official_url,
        commit=commit,
        license=license_value,
        evidence_urls=tuple(sorted(set(evidence_urls))),
        evidence_reason=" | ".join(reason_parts),
        reviewed_at=reviewed_at,
        source_file=source_path.name,
        source_line=line_number,
        detailed_evidence=tuple(detailed_evidence),
        target=target,
    )


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file() or path.is_symlink():
        _fail("invalid_input", f"JSONL input must be a regular non-symlink file: {path}")
    rows: list[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                _fail("invalid_jsonl", f"invalid JSON at {path}:{line_number}: {error}")
            if not isinstance(raw, Mapping):
                _fail("invalid_jsonl", f"JSONL row is not an object at {path}:{line_number}")
            rows.append(raw)
    return rows


def _unit_action_failures(path: Path) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    if not path.is_file() or path.is_symlink():
        _fail("invalid_unit_action_log", f"unit-action log is missing or unsafe: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    marker_counts = [
        int(match.group("count"))
        for line in lines
        if (match := FAILURE_COUNT_RE.fullmatch(line)) is not None
    ]
    if len(marker_counts) != 1:
        _fail(
            "invalid_unit_action_log",
            f"unit-action log must have exactly one failure-count marker: {path}",
        )
    titles = []
    all_failures = []
    for line in lines:
        if not line.startswith("  - "):
            continue
        if ": code " not in line:
            all_failures.append({"failure": line[4:], "kind": "other_action"})
            continue
        title, _, suffix = line[4:].rpartition(": code ")
        if not title or "实际 " not in suffix:
            _fail("invalid_unit_action_log", f"malformed failure line in {path}: {line}")
        actual = suffix.rsplit("实际 ", 1)[1].strip()
        all_failures.append(
            {"exact_title": title, "actual": actual, "kind": "code_status"}
        )
        titles.append(title)
    if marker_counts[0] != len(all_failures):
        _fail(
            "unit_action_count_mismatch",
            "unit-action declared "
            f"{marker_counts[0]} failures but contains {len(all_failures)}: {path}",
        )
    duplicates = sorted(title for title, count in Counter(titles).items() if count > 1)
    if duplicates:
        _fail(
            "duplicate_unit_action_title",
            "duplicate unit-action failure titles",
            titles=duplicates,
        )
    other_failures = tuple(
        item
        for item in all_failures
        if item["kind"] != "code_status" or item["actual"] != "not_provided"
    )
    return tuple(titles), other_failures


def _input_coverage(audit_dir: Path) -> dict[str, list[str]]:
    expected = {spec.audit_name for spec in AUDIT_SPECS}
    actual = {path.name for path in audit_dir.glob("*.jsonl")} if audit_dir.is_dir() else set()
    return {
        "missing_audit_files": sorted(expected - actual),
        "extra_audit_files": sorted(actual - expected),
    }


def _read_audits(audit_dir: Path, unit_actions_dir: Path) -> tuple[list[AuditRow], dict[str, Any]]:
    coverage = _input_coverage(audit_dir)
    if coverage["missing_audit_files"] or coverage["extra_audit_files"]:
        _fail(
            "audit_file_set_mismatch",
            "audit directory must contain exactly the seven declared JSONL files",
            **coverage,
        )
    rows: list[AuditRow] = []
    by_domain: dict[str, Any] = {}
    for spec in AUDIT_SPECS:
        audit_path = audit_dir / spec.audit_name
        unit_path = unit_actions_dir / spec.unit_action_name
        date_match = DATE_PREFIX_RE.match(unit_path.name)
        fallback_reviewed_at = date_match.group("date") if date_match else ""
        if not fallback_reviewed_at:
            _fail("missing_review_date", f"cannot derive review date from {unit_path.name}")
        failures, other_failures = _unit_action_failures(unit_path)
        raw_rows = _read_jsonl(audit_path)
        parsed = [
            _parse_audit_row(
                raw,
                spec=spec,
                source_path=audit_path,
                line_number=index,
                fallback_reviewed_at=fallback_reviewed_at,
            )
            for index, raw in enumerate(raw_rows, start=1)
        ]
        titles = [row.exact_title for row in parsed]
        duplicates = sorted(title for title, count in Counter(titles).items() if count > 1)
        if duplicates:
            _fail(
                "duplicate_audit_title",
                f"duplicate audit titles for {spec.domain}",
                domain=spec.domain,
                titles=duplicates,
            )
        missing = sorted(set(failures) - set(titles))
        extra = sorted(set(titles) - set(failures))
        if missing or extra or len(failures) != len(parsed):
            _fail(
                "audit_failure_coverage_mismatch",
                f"audit rows do not exactly cover unit-action failures for {spec.domain}",
                domain=spec.domain,
                missing_titles=missing,
                extra_titles=extra,
                failure_count=len(failures),
                audit_count=len(parsed),
            )
        rows.extend(parsed)
        by_domain[spec.domain] = {
            "audit_file": str(audit_path.resolve()),
            "unit_action_log": str(unit_path.resolve()),
            "failure_count": len(failures),
            "all_mismatch_count": len(failures)
            + sum(item["kind"] == "other_action" for item in other_failures),
            "not_provided_mismatch_count": len(failures)
            - sum(item["kind"] == "code_status" for item in other_failures),
            "other_mismatches": list(other_failures),
            "audit_count": len(parsed),
            "missing_titles": [],
            "extra_titles": [],
        }
    return rows, by_domain


def _read_resolutions(path: Path | None) -> dict[tuple[str, str], str]:
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail("invalid_catalog_resolutions", f"cannot read catalog resolutions: {error}")
    values = document.get("resolutions") if isinstance(document, Mapping) else None
    if not isinstance(values, list):
        _fail("invalid_catalog_resolutions", "catalog resolutions must contain a list")
    result: dict[tuple[str, str], str] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            _fail("invalid_catalog_resolutions", f"resolution {index} is not an object")
        domain = _clean_text(raw.get("domain"), label="resolution domain", required=True)
        title = _clean_text(raw.get("exact_title"), label="resolution title", required=True)
        asset_id = _clean_text(raw.get("asset_id"), label="resolution asset_id", required=True)
        assert domain is not None and title is not None and asset_id is not None
        key = (domain, title)
        if key in result:
            _fail("duplicate_catalog_resolution", "duplicate catalog resolution", key=list(key))
        result[key] = asset_id
    return result


def _safe_catalog_urls(values: Any, *, label: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        _fail("invalid_catalog", f"{label} must be a list")
    result = []
    for index, value in enumerate(values):
        text = _clean_text(value, label=f"{label}[{index}]", required=True)
        assert text is not None
        try:
            result.append(_safe_evidence_url(text))
        except ValueError as error:
            _fail("unsafe_catalog_url", str(error), field=f"{label}[{index}]")
    return tuple(sorted(set(result)))


def _identifier_values(
    identifiers: Sequence[dict[str, Any]], scheme: str
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(item.get("canonical") or item.get("value")).strip()
                for item in identifiers
                if str(item.get("scheme") or "").casefold() == scheme
                and str(item.get("canonical") or item.get("value") or "").strip()
            }
        )
    )


def _catalog_paper(raw: Mapping[str, Any], *, domain: str) -> CatalogPaper:
    asset_raw = raw.get("asset")
    if not isinstance(asset_raw, Mapping):
        _fail("invalid_catalog", "catalog row has no asset object")
    asset_id = _clean_text(asset_raw.get("asset_id"), label="catalog asset_id", required=True)
    title = _clean_text(asset_raw.get("title"), label="catalog title", required=True)
    assert asset_id is not None and title is not None
    authors_raw = asset_raw.get("authors")
    if not isinstance(authors_raw, list) or not all(
        isinstance(value, str) and value.strip() for value in authors_raw
    ):
        _fail("invalid_catalog", f"catalog authors must be a string list: {asset_id}")
    year_raw = asset_raw.get("year")
    if year_raw is None:
        year = None
    elif isinstance(year_raw, int) and not isinstance(year_raw, bool) and 1000 <= year_raw <= 9999:
        year = year_raw
    else:
        _fail("invalid_catalog", f"catalog year is invalid: {asset_id}")
    identifiers_raw = asset_raw.get("external_identifiers") or []
    if not isinstance(identifiers_raw, list) or not all(
        isinstance(value, Mapping) for value in identifiers_raw
    ):
        _fail("invalid_catalog", f"catalog external_identifiers are invalid: {asset_id}")
    identifiers = tuple(dict(value) for value in identifiers_raw)
    dois = _identifier_values(identifiers, "doi")
    arxiv_ids = _identifier_values(identifiers, "arxiv")
    if len(dois) > 1 or len(arxiv_ids) > 1:
        _fail(
            "conflicting_catalog_identifiers",
            f"catalog asset has conflicting strong identifiers: {asset_id}",
            dois=list(dois),
            arxiv_ids=list(arxiv_ids),
        )
    if year is None and not dois and not arxiv_ids:
        _fail(
            "insufficient_catalog_identity",
            f"catalog asset needs year or a strong identifier for safe enrichment: {asset_id}",
        )
    return CatalogPaper(
        asset_id=asset_id,
        domain=domain,
        title=title,
        title_zh=_clean_text(asset_raw.get("title_zh"), label="catalog title_zh"),
        authors=tuple(value.strip() for value in authors_raw),
        year=year,
        venue=_clean_text(asset_raw.get("venue"), label="catalog venue"),
        source_urls=_safe_catalog_urls(asset_raw.get("source_urls"), label="source_urls"),
        identifiers=identifiers,
        doi=dois[0] if dois else None,
        arxiv_id=arxiv_ids[0] if arxiv_ids else None,
    )


def _bind_catalog(
    rows: Sequence[AuditRow],
    *,
    catalog_path: Path,
    resolutions: Mapping[tuple[str, str], str],
) -> tuple[list[BoundRow], list[dict[str, Any]]]:
    wanted = {(row.domain, row.exact_title) for row in rows}
    candidates: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for raw in _read_jsonl(catalog_path):
        asset = raw.get("asset")
        if not isinstance(asset, Mapping) or asset.get("asset_type") != "paper":
            continue
        if asset.get("deleted_at") is not None:
            continue
        title = asset.get("title")
        domains = asset.get("domains")
        if not isinstance(title, str) or not isinstance(domains, list):
            continue
        for domain in domains:
            key = (domain, title)
            if key in wanted:
                candidates[key].append(raw)

    unknown_resolution_keys = sorted(set(resolutions) - wanted)
    if unknown_resolution_keys:
        _fail(
            "unused_catalog_resolution",
            "catalog resolution contains titles outside the audit set",
            keys=[list(value) for value in unknown_resolution_keys],
        )
    bound = []
    used_resolutions = []
    for row in rows:
        key = (row.domain, row.exact_title)
        matches = candidates.get(key, [])
        selected: Mapping[str, Any] | None = None
        resolution_id = resolutions.get(key)
        if len(matches) == 1 and resolution_id is None:
            selected = matches[0]
        elif resolution_id is not None:
            selected_matches = [
                value
                for value in matches
                if isinstance(value.get("asset"), Mapping)
                and value["asset"].get("asset_id") == resolution_id
            ]
            if len(selected_matches) != 1:
                _fail(
                    "invalid_catalog_resolution",
                    "explicit catalog resolution does not select exactly one exact-title asset",
                    domain=row.domain,
                    exact_title=row.exact_title,
                    asset_id=resolution_id,
                    candidate_asset_ids=[
                        value["asset"].get("asset_id")
                        for value in matches
                        if isinstance(value.get("asset"), Mapping)
                    ],
                )
            selected = selected_matches[0]
            used_resolutions.append(
                {
                    "domain": row.domain,
                    "exact_title": row.exact_title,
                    "asset_id": resolution_id,
                }
            )
        else:
            _fail(
                "catalog_exact_match_failed",
                "catalog title/domain must resolve to exactly one paper asset",
                domain=row.domain,
                exact_title=row.exact_title,
                candidate_count=len(matches),
                candidate_asset_ids=[
                    value["asset"].get("asset_id")
                    for value in matches
                    if isinstance(value.get("asset"), Mapping)
                ],
            )
        assert selected is not None
        paper = _catalog_paper(selected, domain=row.domain)
        if paper.title != row.exact_title:
            _fail("catalog_title_drift", "catalog binding is not an exact title match")
        bound.append(BoundRow(row, paper))
    return bound, used_resolutions


def _duplicate_asset_bindings(rows: Sequence[BoundRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[BoundRow]] = defaultdict(list)
    for row in rows:
        grouped[row.paper.asset_id].append(row)
    duplicates = []
    for asset_id, values in sorted(grouped.items()):
        if len(values) == 1:
            continue
        signatures = {
            (
                value.audit.status,
                value.audit.target.key if value.audit.target else None,
                value.audit.commit,
                value.audit.license,
            )
            for value in values
        }
        if len(signatures) != 1:
            _fail(
                "duplicate_catalog_asset_decision_conflict",
                "one catalog asset has conflicting cross-domain code decisions",
                asset_id=asset_id,
                decisions=[
                    {
                        "domain": value.paper.domain,
                        "status": value.audit.status,
                        "repository": value.audit.target.url if value.audit.target else None,
                        "commit": value.audit.commit,
                        "license": value.audit.license,
                    }
                    for value in values
                ],
            )
        duplicates.append(
            {
                "asset_id": asset_id,
                "exact_title": values[0].paper.title,
                "domains": sorted({value.paper.domain for value in values}),
                "status": values[0].audit.status,
            }
        )
    return duplicates


def _add_catalog_evidence(bound: BoundRow) -> AuditRow:
    urls = tuple(sorted(set((*bound.audit.evidence_urls, *bound.paper.source_urls))))
    if not urls:
        _fail(
            "missing_evidence_url",
            "paper has no safe primary or audit evidence URL",
            asset_id=bound.paper.asset_id,
            exact_title=bound.paper.title,
        )
    return replace(bound.audit, evidence_urls=urls)


def _repository_groups(rows: Sequence[BoundRow]) -> dict[str, RepositoryGroup]:
    groups: dict[str, RepositoryGroup] = {}
    for original in rows:
        audit = _add_catalog_evidence(original)
        row = BoundRow(audit, original.paper)
        if audit.status != "official_public":
            continue
        assert audit.target is not None and audit.commit is not None and audit.license is not None
        group = groups.get(audit.target.key)
        if group is None:
            group = RepositoryGroup(audit.target, audit.commit, audit.license)
            groups[audit.target.key] = group
        else:
            if group.commit != audit.commit:
                _fail(
                    "repository_commit_conflict",
                    "same base repository has conflicting fixed commits",
                    repository=audit.target.url,
                    commits=sorted({group.commit, audit.commit}),
                )
            if group.license != audit.license:
                _fail(
                    "repository_license_conflict",
                    "same repository commit has conflicting license decisions",
                    repository=audit.target.url,
                    licenses=sorted({group.license, audit.license}),
                )
        group.rows.append(row)
        if audit.target.sparse_path is None:
            group.requires_full_checkout = True
        else:
            group.sparse_paths.add(audit.target.sparse_path)
    return groups


def _repository_approval(group: RepositoryGroup) -> dict[str, Any]:
    sparse_paths = [] if group.requires_full_checkout else sorted(group.sparse_paths)
    evidence: list[str] = []
    paper_evidence: list[dict[str, Any]] = []
    by_asset: dict[str, list[BoundRow]] = defaultdict(list)
    for bound in group.rows:
        by_asset[bound.paper.asset_id].append(bound)
    for asset_id, values in sorted(by_asset.items()):
        bound = values[0]
        audits = [value.audit for value in values]
        audit = audits[0]
        target = audit.target
        assert target is not None
        evidence.extend(f"[{asset_id}] {value.evidence_reason}" for value in audits)
        merged_urls = sorted({url for value in audits for url in value.evidence_urls})
        evidence.extend(f"[{asset_id}] {url}" for url in merged_urls)
        paper_evidence.append(
            {
                "paper_id": asset_id,
                "research_status": "official_public",
                "reviewed_at": audit.reviewed_at,
                "license": audit.license,
                "source_batch": audit.source_file,
                "source_line": audit.source_line,
                "original_official_url": audit.official_url,
                "tree_ref": target.tree_ref,
                "sparse_path": target.sparse_path,
                "evidence_urls": merged_urls,
                "evidence_reason": " | ".join(
                    sorted({value.evidence_reason for value in audits})
                ),
            }
        )
    return {
        "url": group.target.url,
        "official": True,
        "commit": group.commit,
        "destination": group.target.destination,
        "papers": [
            {
                "paper_id": bound.paper.asset_id,
                "exact_title": bound.paper.title,
                "paper_record_title": bound.paper.title,
                "title_match": "exact",
            }
            for bound in sorted(
                (values[0] for values in by_asset.values()),
                key=lambda value: value.paper.asset_id,
            )
        ],
        "sparse_paths": sparse_paths,
        "evidence": sorted(set(evidence)),
        "licenses": [group.license],
        "paper_evidence": paper_evidence,
    }


def _no_code_approval(bound: BoundRow) -> dict[str, Any]:
    audit = _add_catalog_evidence(bound)
    return {
        "paper_id": bound.paper.asset_id,
        "exact_title": bound.paper.title,
        "paper_record_title": bound.paper.title,
        "title_match": "exact",
        "verified_no_public_code": True,
        "reviewed_at": audit.reviewed_at,
        "research_status": audit.status,
        "announced_url": None,
        "announced_commit": None,
        "license": audit.license,
        "evidence_reason": audit.evidence_reason,
        "source_batch": audit.source_file,
        "source_line": audit.source_line,
        "evidence_urls": list(audit.evidence_urls),
    }


def _no_code_approvals(rows: Sequence[BoundRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[BoundRow]] = defaultdict(list)
    for row in rows:
        if row.audit.status in NON_PUBLIC_STATUSES:
            grouped[row.paper.asset_id].append(row)
    approvals = []
    for _, values in sorted(grouped.items()):
        approval = _no_code_approval(values[0])
        audits = [_add_catalog_evidence(value) for value in values]
        approval["evidence_urls"] = sorted(
            {url for audit in audits for url in audit.evidence_urls}
        )
        approval["evidence_reason"] = " | ".join(
            sorted({audit.evidence_reason for audit in audits})
        )
        approval["source_batch"] = ",".join(
            sorted({audit.source_file for audit in audits})
        )
        approvals.append(approval)
    return approvals


def _source_record_id(asset_id: str) -> str:
    candidate = f"code-asset-{asset_id.casefold()}"
    if SOURCE_RECORD_SAFE_RE.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()
    return f"code-asset-sha256-{digest}"


def _manifest_item(bound: BoundRow) -> dict[str, Any]:
    audit = bound.audit
    paper = bound.paper
    target = audit.target
    assert target is not None and audit.commit is not None and audit.license is not None
    source_urls = sorted(set((*paper.source_urls, target.url)))
    return {
        "id": _source_record_id(paper.asset_id),
        "title": paper.title,
        "title_zh": paper.title_zh,
        "authors": list(paper.authors),
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "tags": ["existing-code-enrichment", paper.domain],
        "source_urls": source_urls,
        "representations": {
            "code": {
                "path": target.destination,
                "url": target.url,
                "commit": audit.commit,
                "license": audit.license,
                "status": "official_local",
            }
        },
        "provenance": {
            "catalog_asset_id": paper.asset_id,
            "code_research_status": "official_public",
            "code_materialization_status": "official_local",
            "original_official_url": audit.official_url,
            "tree_ref": target.tree_ref,
            "sparse_paths": [target.sparse_path] if target.sparse_path else [],
            "audit_source": f"{audit.source_file}:{audit.source_line}",
        },
    }


def _paper_record(bound: BoundRow) -> dict[str, Any]:
    paper = bound.paper
    return {
        "paper_id": paper.asset_id,
        "title": paper.title,
        "authors": list(paper.authors),
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "domain_slugs": [paper.domain],
        "code": {"status": "unknown"},
        "catalog_identifiers": list(paper.identifiers),
    }


def _paper_records(rows: Sequence[BoundRow]) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, list[BoundRow]] = defaultdict(list)
    for row in rows:
        grouped[row.paper.asset_id].append(row)
    records = []
    for _, values in sorted(grouped.items()):
        record = _paper_record(values[0])
        record["domain_slugs"] = sorted({value.paper.domain for value in values})
        records.append(record)
    return tuple(records)


def _load_current_allowlist(path: Path | None) -> dict[str, set[str]]:
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        _fail("invalid_allowlist", f"cannot read current allowlist: {error}")
    if not isinstance(raw, Mapping):
        _fail("invalid_allowlist", "current allowlist must be a mapping")
    result: dict[str, set[str]] = {}
    for domain, value in raw.items():
        if not isinstance(domain, str) or not isinstance(value, Mapping):
            _fail("invalid_allowlist", "allowlist domain entries must be mappings")
        titles = value.get("no_code_titles") or []
        if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
            _fail("invalid_allowlist", f"invalid no_code_titles for {domain}")
        result[domain] = {title for title in titles}
    return result


def _source_plan(
    *,
    domain: str,
    expected_records: int,
    archive_root: Path,
    batch_id: str,
) -> dict[str, Any]:
    source_id = f"existing-code-{domain}-{batch_id}"
    return {
        "source_id": source_id,
        "domain": domain,
        "register_source": expected_records > 0,
        "target_manifest": f"config/intake/{source_id}/manifest.json",
        "source": {
            "id": source_id,
            "label": f"{domain} · 存量论文官方代码补录 · {batch_id}",
            "domain": domain,
            "adapter": "generic_manifest",
            "root": f"config/intake/{source_id}",
            "manifest": "manifest.json",
            "expected_records": expected_records,
            "read_only": True,
            "asset_roots": {"repository": archive_root.as_posix()},
            "options": {
                "record_selector": "items",
                "field_mapping": {
                    "source_record_id": "id",
                    "title": "title",
                    "title_zh": "title_zh",
                    "authors": "authors",
                    "year": "year",
                    "venue": "venue",
                    "doi": "doi",
                    "arxiv": "arxiv_id",
                    "tags": "tags",
                    "source_urls": "source_urls",
                },
                "representations": [
                    {
                        "type": "repository",
                        "role": "official",
                        "root_key": "repository",
                        "path_field": "representations.code.path",
                        "url_field": "representations.code.url",
                        "commit_field": "representations.code.commit",
                        "license_field": "representations.code.license",
                        "status_field": "representations.code.status",
                    }
                ],
            },
        },
    }


def _validate_archiver_contract(
    approvals: Mapping[str, Any], paper_records: Sequence[Mapping[str, Any]], core_root: Path
) -> None:
    with tempfile.TemporaryDirectory(prefix="existing-code-materializer-contract-") as directory:
        root = Path(directory)
        records_path = root / "paper-records.jsonl"
        approvals_path = root / "approvals.json"
        records_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in paper_records),
            encoding="utf-8",
        )
        approvals_path.write_text(
            json.dumps(approvals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        # Import dynamically so this planner remains type-checkable in isolation from the
        # much older audit module while still exercising its real runtime parser.
        audit_module = cast(Any, import_module("tools.paper_code_audit.audit"))
        papers = audit_module.load_paper_records([records_path], core_root=core_root)
        audit_module.load_approvals(approvals_path, papers)


def build_plan(
    *,
    audit_dir: Path,
    unit_actions_dir: Path,
    catalog_path: Path,
    core_root: Path,
    current_allowlist_path: Path | None = None,
    catalog_resolutions_path: Path | None = None,
    batch_id: str = DEFAULT_BATCH_ID,
) -> BuildResult:
    """Build a complete dry-run plan without network or source/config mutation."""

    if not SAFE_BATCH_RE.fullmatch(batch_id):
        _fail("invalid_batch_id", "batch_id must be a lowercase portable identifier")
    core = core_root.expanduser().resolve(strict=True)
    archive_root = (core / ARCHIVE_SUBPATH).resolve(strict=False)
    try:
        archive_root.relative_to(core)
    except ValueError:
        _fail("unsafe_archive_root", "archive root escapes core root")
    audit_root = audit_dir.expanduser().resolve(strict=True)
    unit_root = unit_actions_dir.expanduser().resolve(strict=True)
    catalog = catalog_path.expanduser().resolve(strict=True)
    rows, coverage_by_domain = _read_audits(audit_root, unit_root)
    resolutions = _read_resolutions(catalog_resolutions_path)
    bound_rows, used_resolutions = _bind_catalog(
        rows, catalog_path=catalog, resolutions=resolutions
    )
    duplicate_asset_bindings = _duplicate_asset_bindings(bound_rows)
    groups = _repository_groups(bound_rows)
    repository_approvals = [
        _repository_approval(group) for _, group in sorted(groups.items())
    ]
    no_code = _no_code_approvals(bound_rows)
    paper_records = _paper_records(bound_rows)
    status_counts = Counter(bound.audit.status for bound in bound_rows)
    duplicate_remotes = [
        {
            "canonical_remote": group.target.url,
            "commit": group.commit,
            "paper_asset_ids": sorted(bound.paper.asset_id for bound in group.rows),
            "paper_count": len(group.rows),
        }
        for _, group in sorted(groups.items())
        if len(group.rows) > 1
    ]
    clone_plan = [
        {
            "url": approval["url"],
            "clone_url": f"{approval['url']}.git",
            "commit": approval["commit"],
            "destination": approval["destination"],
            "absolute_destination": str(archive_root / PurePosixPath(approval["destination"])),
            "sparse_paths": approval["sparse_paths"],
            "paper_count": len(approval["papers"]),
            "action": "approved_not_applied",
            "executor": "tools.paper_code_audit",
        }
        for approval in repository_approvals
    ]

    manifests: dict[str, dict[str, Any]] = {}
    source_plan = []
    source_expected_counts: dict[str, int] = {}
    for spec in AUDIT_SPECS:
        public = sorted(
            (
                bound
                for bound in bound_rows
                if bound.paper.domain == spec.domain and bound.audit.status == "official_public"
            ),
            key=lambda value: value.paper.asset_id,
        )
        manifests[spec.domain] = {"items": [_manifest_item(bound) for bound in public]}
        source_expected_counts[spec.domain] = len(public)
        source_plan.append(
            _source_plan(
                domain=spec.domain,
                expected_records=len(public),
                archive_root=archive_root,
                batch_id=batch_id,
            )
        )

    current_allowlist = _load_current_allowlist(current_allowlist_path)
    allowlist_additions: dict[str, tuple[str, ...]] = {}
    already_allowlisted: dict[str, list[str]] = {}
    official_allowlist_conflicts: dict[str, list[str]] = {}
    for spec in AUDIT_SPECS:
        current = current_allowlist.get(spec.domain, set())
        non_public_titles = {
            bound.paper.title
            for bound in bound_rows
            if bound.paper.domain == spec.domain and bound.audit.status in NON_PUBLIC_STATUSES
        }
        official_titles = {
            bound.paper.title
            for bound in bound_rows
            if bound.paper.domain == spec.domain and bound.audit.status == "official_public"
        }
        conflicts = sorted(official_titles & current)
        if conflicts:
            official_allowlist_conflicts[spec.domain] = conflicts
        allowlist_additions[spec.domain] = tuple(sorted(non_public_titles - current))
        already_allowlisted[spec.domain] = sorted(non_public_titles & current)
    if official_allowlist_conflicts:
        _fail(
            "official_code_allowlist_conflict",
            "official_public papers are already declared as no-code",
            conflicts=official_allowlist_conflicts,
        )

    no_code_evidence = tuple(
        {
            "domain": bound.paper.domain,
            "paper_asset_id": bound.paper.asset_id,
            "exact_title": bound.paper.title,
            "research_status": bound.audit.status,
            "reviewed_at": bound.audit.reviewed_at,
            "evidence_urls": list(_add_catalog_evidence(bound).evidence_urls),
            "evidence_reason": bound.audit.evidence_reason,
            "detailed_evidence": list(bound.audit.detailed_evidence),
            "source_batch": bound.audit.source_file,
            "source_line": bound.audit.source_line,
            "allowlist_action": (
                "already_present"
                if bound.paper.title in current_allowlist.get(bound.paper.domain, set())
                else "add_exact_title"
            ),
        }
        for bound in sorted(bound_rows, key=lambda value: value.paper.asset_id)
        if bound.audit.status in NON_PUBLIC_STATUSES
    )
    approvals = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "repositories": repository_approvals,
        "no_code": no_code,
        "audit": {
            "compiler": "tools.existing_code_materializer",
            "plan_schema": PLAN_SCHEMA,
            "research_rows": len(bound_rows),
            "status_counts": dict(sorted(status_counts.items())),
            "unique_repositories": len(repository_approvals),
            "duplicate_remotes": duplicate_remotes,
            "clone_plan": clone_plan,
            "archive_root": str(archive_root),
            "network_or_clone_performed": False,
        },
    }
    _validate_archiver_contract(approvals, paper_records, core)

    report = {
        "schema": REPORT_SCHEMA,
        "status": "passed",
        "dry_run": True,
        "network_or_clone_performed": False,
        "config_or_database_modified": False,
        "archive_root": str(archive_root),
        "input_file_coverage": {
            "expected_audit_files": [spec.audit_name for spec in AUDIT_SPECS],
            "missing_audit_files": [],
            "extra_audit_files": [],
            "domains": coverage_by_domain,
        },
        "catalog": {
            "path": str(catalog),
            "binding": "exact_title_and_domain",
            "bound_paper_assets": len(bound_rows),
            "unique_paper_assets": len(paper_records),
            "cross_domain_asset_bindings": duplicate_asset_bindings,
            "explicit_asset_resolutions": used_resolutions,
            "weak_or_ambiguous_matches": 0,
        },
        "counts": {
            "audit_rows": len(bound_rows),
            "paper_records": len(paper_records),
            "status": dict(sorted(status_counts.items())),
            "official_public_papers": status_counts["official_public"],
            "non_public_papers": sum(status_counts[value] for value in NON_PUBLIC_STATUSES),
            "unique_repositories": len(repository_approvals),
            "duplicate_remote_groups": len(duplicate_remotes),
            "allowlist_additions": sum(len(value) for value in allowlist_additions.values()),
            "allowlist_already_present": sum(len(value) for value in already_allowlisted.values()),
        },
        "duplicate_remotes": duplicate_remotes,
        "missing": [],
        "extra": [],
        "source_expected_counts": source_expected_counts,
        "allowlist": {
            "additions_by_domain": {
                domain: list(titles) for domain, titles in allowlist_additions.items()
            },
            "already_present_by_domain": already_allowlisted,
        },
        "outputs_are_drafts": True,
        "next_executor": "tools.paper_code_audit",
    }
    return BuildResult(
        report=report,
        approvals=approvals,
        paper_records=paper_records,
        manifests=manifests,
        source_plan=tuple(source_plan),
        allowlist_additions=allowlist_additions,
        no_code_evidence=no_code_evidence,
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _allowlist_yaml(additions: Mapping[str, Sequence[str]]) -> str:
    lines = [
        "# DRAFT ONLY: merge reviewed exact titles into config/check-unit-actions-allowlist.yaml",
        "# Generated without modifying Hub configuration or cloning repositories.",
    ]
    for spec in AUDIT_SPECS:
        lines.append(f"{spec.domain}:")
        titles = additions.get(spec.domain, ())
        if titles:
            lines.append("  no_code_titles:")
            lines.extend(f"    - {_yaml_quote(title)}" for title in titles)
        else:
            lines.append("  no_code_titles: []")
    return "\n".join(lines) + "\n"


def _source_plan_yaml(source_plan: Sequence[Mapping[str, Any]]) -> str:
    document = {
        "schema": "existing-code-materializer-source-plan/v1",
        "draft_only": True,
        "sources": [value["source"] for value in source_plan if value["register_source"]],
        "zero_record_domains": [
            value["domain"] for value in source_plan if not value["register_source"]
        ],
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def _summary_markdown(report: Mapping[str, Any]) -> str:
    counts = cast(Mapping[str, Any], report.get("counts") or {})
    lines = [
        "# 存量领域论文代码物化 dry-run",
        "",
        f"- 状态：`{report.get('status')}`",
        f"- 审计论文：{counts.get('audit_rows', 0)}",
        f"- 官方公开代码：{counts.get('official_public_papers', 0)}",
        f"- 无官方公开代码：{counts.get('non_public_papers', 0)}",
        f"- 唯一官方仓库：{counts.get('unique_repositories', 0)}",
        f"- 待加入 no-code 白名单：{counts.get('allowlist_additions', 0)}",
        "- 网络/clone：未执行",
        "- Hub 配置/数据库：未修改",
        "",
        "## 分领域预期记录数",
        "",
        "| 领域 | 官方代码 manifest 记录 |",
        "| --- | ---: |",
    ]
    expected = cast(Mapping[str, Any], report.get("source_expected_counts") or {})
    lines.extend(f"| `{spec.domain}` | {expected.get(spec.domain, 0)} |" for spec in AUDIT_SPECS)
    duplicates = cast(Sequence[Mapping[str, Any]], report.get("duplicate_remotes") or [])
    lines.extend(["", "## 共享官方仓库", ""])
    if duplicates:
        for item in duplicates:
            lines.append(
                f"- `{item['canonical_remote']}` @ `{item['commit']}`："
                f"{item['paper_count']} 篇论文复用"
            )
    else:
        lines.append("- 无跨论文共享 remote。")
    lines.extend(
        [
            "",
            "## 安全边界",
            "",
            "本产物只是计划。后续 clone 必须由 `tools.paper_code_audit` 读取 approvals 执行，",
            "以保留 fixed-commit、LFS skip、non-cone sparse、Windows 路径和 reparse 安全门禁。",
        ]
    )
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        _fail("unsafe_output", f"output parent is a symlink: {path.parent}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def write_plan(result: BuildResult, output_dir: Path) -> dict[str, str]:
    """Atomically write all draft artifacts below one explicit output directory."""

    root = output_dir.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        _fail("unsafe_output", f"output directory is unsafe: {root}")
    paths = {
        "report_json": root / "summary.json",
        "report_markdown": root / "summary.md",
        "approvals": root / "paper-code-approvals.json",
        "paper_records": root / "paper-records.jsonl",
        "allowlist_additions": root / "allowlist-additions.yaml",
        "no_code_evidence": root / "no-code-evidence.json",
        "source_plan": root / "hub-source-plan.yaml",
    }
    _atomic_write(paths["report_json"], _json_text(result.report))
    _atomic_write(paths["report_markdown"], _summary_markdown(result.report))
    _atomic_write(paths["approvals"], _json_text(result.approvals))
    _atomic_write(
        paths["paper_records"],
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in result.paper_records),
    )
    _atomic_write(paths["allowlist_additions"], _allowlist_yaml(result.allowlist_additions))
    _atomic_write(paths["no_code_evidence"], _json_text(list(result.no_code_evidence)))
    _atomic_write(paths["source_plan"], _source_plan_yaml(result.source_plan))
    for source in result.source_plan:
        source_id = str(source["source_id"])
        domain = str(source["domain"])
        _atomic_write(
            root / "manifests" / source_id / "manifest.json",
            _json_text(result.manifests[domain]),
        )
    return {key: str(path) for key, path in paths.items()}


def _write_failure_report(
    *, output_dir: Path, error: MaterializerError, audit_dir: Path
) -> None:
    coverage = _input_coverage(audit_dir)
    report = {
        "schema": REPORT_SCHEMA,
        "status": "failed",
        "dry_run": True,
        "network_or_clone_performed": False,
        "config_or_database_modified": False,
        "error": {"code": error.code, "message": str(error), "details": error.details},
        "input_file_coverage": coverage,
        "counts": {},
        "missing": coverage["missing_audit_files"],
        "extra": coverage["extra_audit_files"],
        "source_expected_counts": {},
    }
    root = output_dir.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / "summary.json", _json_text(report))
    _atomic_write(root / "summary.md", _summary_markdown(report))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a fail-closed, no-network plan for existing-paper code materialization."
    )
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--unit-actions-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--current-allowlist", type=Path)
    parser.add_argument("--catalog-resolutions", type=Path)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = build_plan(
            audit_dir=arguments.audit_dir,
            unit_actions_dir=arguments.unit_actions_dir,
            catalog_path=arguments.catalog,
            core_root=arguments.core_root,
            current_allowlist_path=arguments.current_allowlist,
            catalog_resolutions_path=arguments.catalog_resolutions,
            batch_id=arguments.batch_id,
        )
        outputs = write_plan(result, arguments.output_dir)
    except MaterializerError as error:
        _write_failure_report(
            output_dir=arguments.output_dir,
            error=error,
            audit_dir=arguments.audit_dir,
        )
        print(json.dumps({"status": "failed", "error": error.code}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "passed",
                "outputs": outputs,
                "clone_performed": False,
                "config_or_database_modified": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
