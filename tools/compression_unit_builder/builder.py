from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TOOL_DIR = Path(__file__).resolve().parent
HUB_ROOT = TOOL_DIR.parents[1]
HTML_TOOL_DIR = HUB_ROOT / "tools/paper_intake_html"
VALIDATOR_SOURCE = TOOL_DIR / "validate_unit.py"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}|[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")

DOMAIN_CONFIG: dict[str, dict[str, str]] = {
    "video-llm-compression": {
        "label": "VideoLLM / 视频压缩",
        "directory": "VLM框架3DV压缩",
        "readme": "README.v2.md",
        "entry": "index.v2.html",
        "site_index": "library/site/index.v2.html",
        "manifest": "outputs/manifest.v2.json",
        "validator": "scripts/validate_unit_v2.py",
        "analysis_root": "library/analysis/v2",
        "evidence_root": "library/evidence/v2",
    },
    "embodied-ai-compression": {
        "label": "具身智能压缩",
        "directory": "具身智能压缩",
        "readme": "README.md",
        "entry": "index.html",
        "site_index": "library/site/index.html",
        "manifest": "outputs/manifest.json",
        "validator": "scripts/validate_unit.py",
        "analysis_root": "library/analysis",
        "evidence_root": "library/evidence",
    },
    "navigation-compression": {
        "label": "导航压缩",
        "directory": "导航压缩",
        "readme": "README.md",
        "entry": "index.html",
        "site_index": "library/site/index.html",
        "manifest": "outputs/manifest.json",
        "validator": "scripts/validate_unit.py",
        "analysis_root": "library/analysis",
        "evidence_root": "library/evidence",
    },
}

DEFAULT_EXPECTATIONS: dict[str, dict[str, int]] = {
    "video-llm-compression": {
        "baseline_canonical": 33,
        "source_records": 95,
        "exact_reuse": 4,
        "alternate_versions": 9,
        "recovered_versions": 1,
        "version_pages": 10,
        "canonical_papers": 115,
        "generated_html": 96,
        "baseline_official_local": 28,
        "baseline_announced_pending": 2,
        "baseline_none_found": 3,
    },
    "embodied-ai-compression": {
        "baseline_canonical": 0,
        "source_records": 44,
        "exact_reuse": 0,
        "alternate_versions": 0,
        "recovered_versions": 0,
        "version_pages": 0,
        "canonical_papers": 44,
        "generated_html": 44,
        "baseline_official_local": 0,
        "baseline_announced_pending": 0,
        "baseline_none_found": 0,
    },
    "navigation-compression": {
        "baseline_canonical": 0,
        "source_records": 20,
        "exact_reuse": 0,
        "alternate_versions": 0,
        "recovered_versions": 0,
        "version_pages": 0,
        "canonical_papers": 20,
        "generated_html": 20,
        "baseline_official_local": 0,
        "baseline_announced_pending": 0,
        "baseline_none_found": 0,
    },
}

INDEX_CSS = """\
:root {
  color-scheme: light;
  --ink: #17212b;
  --muted: #5d6b78;
  --paper: #fffdf8;
  --line: #d9d4c7;
  --accent: #275d87;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f2efe8;
  color: var(--ink);
  font: 16px/1.65 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
main {
  width: min(1180px, calc(100% - 32px));
  margin: 32px auto 64px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: clamp(20px, 4vw, 48px);
  box-shadow: 0 18px 50px #33415518;
}
h1 { font-size: clamp(28px, 5vw, 46px); line-height: 1.15; margin: .2em 0; }
.meta { color: var(--muted); margin-bottom: 28px; }
.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin: 24px 0;
}
.stat { border: 1px solid var(--line); border-radius: 12px; padding: 14px; }
.stat strong { display: block; font-size: 1.6rem; color: var(--accent); }
table { border-collapse: collapse; width: 100%; font-size: .94rem; }
th, td {
  border-bottom: 1px solid var(--line);
  padding: 10px 8px;
  text-align: left;
  vertical-align: top;
}
th { position: sticky; top: 0; background: var(--paper); }
a { color: var(--accent); }
code { font-size: .88em; }
.scroll { overflow: auto; }
.badge {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 1px 8px;
  color: var(--muted);
}
@media(max-width: 640px) {
  main { width: min(100% - 16px, 1180px); margin: 8px auto; padding: 18px; }
  .hide-small { display: none; }
}
"""


class BuildError(RuntimeError):
    """Raised for an unsafe or structurally invalid unit build."""


@dataclass(frozen=True)
class UnitBuild:
    domain: str
    unit_root: Path
    staging_root: Path
    manifest_path: Path
    counts: dict[str, int]
    blockers: list[str]
    warnings: list[str]
    generated_html: tuple[str, ...]
    planned_files: tuple[str, ...]


@dataclass(frozen=True)
class BuildResult:
    status: str
    build_id: str
    staging_root: Path
    units: tuple[UnitBuild, ...]
    applied: bool
    files_created: int = 0
    files_updated: int = 0
    files_unchanged: int = 0


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, _json_bytes(value))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _ensure_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    if not _inside(resolved, root):
        raise BuildError(f"{label} 越出核心领域根：{path}")
    return resolved


def _safe_relative(path: Path, root: Path, label: str) -> str:
    resolved = _ensure_inside(path, root, label)
    return resolved.relative_to(root.resolve(strict=True)).as_posix()


def _relative_link(target: Path, html_path: Path) -> str:
    return os.path.relpath(target, start=html_path.parent).replace("\\", "/")


def _load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise BuildError(f"paper-record JSONL 第 {line_number} 行损坏：{exc}") from exc
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = next(
                (payload[key] for key in ("records", "papers", "items") if key in payload),
                [payload],
            )
        else:
            raise BuildError("paper-record 输入顶层必须是对象或数组。")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BuildError("paper-record 集合必须是对象数组。")
    return [dict(row) for row in rows]


def _load_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        values: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BuildError(f"{path} 第 {line_number} 行 JSON 损坏：{exc}") from exc
            if not isinstance(value, dict):
                raise BuildError(f"{path} 第 {line_number} 行不是对象。")
            values.append(value)
        return values
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        selected = next(
            (payload[key] for key in ("papers", "records", "items", "results") if key in payload),
            None,
        )
        values = selected if selected is not None else [payload]
    else:
        raise BuildError(f"{path} 顶层必须是对象或数组。")
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise BuildError(f"{path} 记录集合不是对象数组。")
    return [dict(item) for item in values]


def _record_key(record: Mapping[str, Any]) -> str:
    value = record.get("source_record_id") or record.get("paper_id") or record.get("id")
    return str(value).strip() if value is not None else ""


def _paper_id(record: Mapping[str, Any]) -> str:
    value = record.get("paper_id") or record.get("id")
    return str(value).strip() if value is not None else ""


def _load_evidence(evidence_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    evidence: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    if not evidence_dir.is_dir():
        raise BuildError(f"证据目录不存在：{evidence_dir}")
    for path in sorted(evidence_dir.rglob("*.json")):
        if path.name in {"batch-report.json", "checkpoint.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"证据 JSON 无法读取：{path}: {exc}") from exc
        if not isinstance(payload, dict):
            warnings.append(f"evidence_not_object:{path}")
            continue
        paper_section = payload.get("paper")
        paper_section = paper_section if isinstance(paper_section, Mapping) else {}
        key = str(
            payload.get("source_record_id")
            or payload.get("paper_id")
            or paper_section.get("paper_id")
            or ""
        ).strip()
        if not key:
            warnings.append(f"evidence_key_missing:{path}")
            continue
        if key in evidence and _json_bytes(evidence[key]) != _json_bytes(payload):
            raise BuildError(f"同一证据键出现冲突内容：{key}")
        evidence[key] = payload
    return evidence, warnings


def _load_code_audit(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    audit: dict[str, dict[str, Any]] = {}
    for path in paths:
        for item in _load_json_or_jsonl(path):
            key = str(item.get("source_record_id") or item.get("paper_id") or "").strip()
            if not key:
                continue
            if key in audit and _json_bytes(audit[key]) != _json_bytes(item):
                raise BuildError(f"代码审计对 {key} 给出冲突记录。请先合并为唯一最终结论。")
            audit[key] = item
    return audit


def _load_version_recoveries(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    recoveries: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "recoveries" in payload:
            values = payload["recoveries"]
        elif isinstance(payload, list):
            values = payload
        else:
            values = [payload]
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise BuildError(f"版本恢复报告不是对象或对象数组：{path}")
        for item in values:
            if item.get("schema_version") != "paper-version-recovery/v1":
                raise BuildError(f"版本恢复报告 schema 不受支持：{path}")
            paper_id = str(item.get("paper_id") or "").strip()
            if not paper_id or not SAFE_ID_RE.fullmatch(paper_id):
                raise BuildError(f"版本恢复报告 paper_id 无效：{path}:{paper_id}")
            if paper_id in recoveries and _json_bytes(recoveries[paper_id]) != _json_bytes(item):
                raise BuildError(f"同一论文出现冲突版本恢复报告：{paper_id}")
            recoveries[paper_id] = dict(item)
    return recoveries


def _load_expectations(path: Path | None) -> dict[str, dict[str, int]]:
    raw: Any = (
        DEFAULT_EXPECTATIONS if path is None else json.loads(path.read_text(encoding="utf-8"))
    )
    if isinstance(raw, dict) and "domains" in raw:
        raw = raw["domains"]
    if not isinstance(raw, dict):
        raise BuildError("expectations 必须是 domain -> counts 对象。")
    result: dict[str, dict[str, int]] = {}
    for domain in DOMAIN_CONFIG:
        values = raw.get(domain)
        if not isinstance(values, dict):
            raise BuildError(f"expectations 缺少领域：{domain}")
        result[domain] = {}
        for key in (
            "baseline_canonical",
            "source_records",
            "exact_reuse",
            "alternate_versions",
            "recovered_versions",
            "version_pages",
            "canonical_papers",
            "generated_html",
            "baseline_official_local",
            "baseline_announced_pending",
            "baseline_none_found",
        ):
            value = values.get(key)
            if not isinstance(value, int) or value < 0:
                raise BuildError(f"expectations.{domain}.{key} 必须是非负整数。")
            result[domain][key] = value
    return result


def _load_v1(unit_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    papers_path = unit_root / "library/catalog/papers.v1.json"
    repositories_path = unit_root / "library/catalog/repositories.v1.json"
    if not papers_path.is_file():
        raise BuildError(f"VLM v1 catalog 不存在：{papers_path}")
    payload = json.loads(papers_path.read_text(encoding="utf-8"))
    papers = payload.get("papers") if isinstance(payload, dict) else None
    if not isinstance(papers, list) or not all(isinstance(item, dict) for item in papers):
        raise BuildError("VLM papers.v1.json 缺少 papers 对象数组。")
    repositories: list[dict[str, Any]] = []
    if repositories_path.is_file():
        repo_payload = json.loads(repositories_path.read_text(encoding="utf-8"))
        candidate = repo_payload.get("repositories") if isinstance(repo_payload, dict) else None
        if isinstance(candidate, list) and all(isinstance(item, dict) for item in candidate):
            repositories = [dict(item) for item in candidate]
    return [dict(item) for item in papers], repositories


def _canonical_repository_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("git@") and ":" in text:
        host, path = text[4:].split(":", 1)
    else:
        parsed = urlparse(text)
        if parsed.scheme.lower() not in {"http", "https", "ssh", "git"}:
            return None
        if not parsed.hostname:
            return None
        host = parsed.hostname
        path = parsed.path
    normalized_path = re.sub(r"/+", "/", path).strip("/")
    if normalized_path.lower().endswith(".git"):
        normalized_path = normalized_path[:-4]
    if not normalized_path:
        return None
    return f"{host.lower()}/{normalized_path.lower()}"


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _inspect_repository(
    path: Path,
    *,
    core_root: Path,
    expected_url: str,
    expected_commit: str,
) -> dict[str, Any]:
    errors: list[str] = []
    if not _inside(path, core_root) or not path.is_dir():
        return {
            "ready": False,
            "path": str(path),
            "errors": ["repository_missing_or_outside_core"],
        }
    remote_result = _run_git(path, "remote", "get-url", "origin")
    commit_result = _run_git(path, "rev-parse", "HEAD")
    status_result = _run_git(path, "status", "--porcelain", "--untracked-files=all")
    remote = remote_result.stdout.strip()
    commit = commit_result.stdout.strip().lower()
    dirty_lines = [line for line in status_result.stdout.splitlines() if line.strip()]
    if remote_result.returncode != 0:
        errors.append("origin_remote_unavailable")
    elif _canonical_repository_key(remote) != _canonical_repository_key(expected_url):
        errors.append("origin_remote_mismatch")
    if commit_result.returncode != 0 or commit != expected_commit.lower():
        errors.append("fixed_commit_mismatch")
    if status_result.returncode != 0:
        errors.append("git_status_unavailable")
    elif dirty_lines:
        errors.append("worktree_not_clean")
    return {
        "ready": not errors,
        "path": str(path),
        "remote": _sanitize_url(remote),
        "commit": commit or None,
        "dirty": bool(dirty_lines),
        "dirty_entries": dirty_lines[:20],
        "errors": errors,
    }


def _baseline_replacement_candidate(
    audit: Mapping[str, Any] | None,
    *,
    unit_root: Path,
    core_root: Path,
) -> tuple[Path, str, str, str | None, list[str]] | None:
    if audit is None:
        return None
    status = str(audit.get("status") or "").lower()
    if status not in {"official_local", "official_public"}:
        return None
    selected = audit.get("selected_local_archive")
    selected = selected if isinstance(selected, dict) else {}
    selected_evidence = selected.get("evidence")
    selected_evidence = selected_evidence if isinstance(selected_evidence, Mapping) else {}
    local_value = _first_string(
        audit.get("local_path"),
        audit.get("archive_path"),
        selected.get("path"),
        selected.get("local_path"),
    )
    url = _sanitize_url(
        _first_string(
            audit.get("official_url"),
            selected.get("url"),
            (audit.get("official_urls") or [None])[0]
            if isinstance(audit.get("official_urls"), list)
            else None,
        )
    )
    commit = _first_string(
        audit.get("commit"),
        selected.get("commit"),
        selected.get("expected_commit"),
        selected_evidence.get("commit"),
    )
    license_value = _first_string(
        audit.get("license"),
        selected.get("license"),
        selected.get("effective_license"),
        (selected_evidence.get("license") or {}).get("status")
        if isinstance(selected_evidence.get("license"), Mapping)
        else None,
    )
    if not local_value or not url or not commit or not COMMIT_RE.fullmatch(commit.lower()):
        return None
    candidate = Path(local_value)
    candidate = candidate if candidate.is_absolute() else unit_root / candidate
    candidate = _ensure_inside(candidate, core_root, "VLM v1 共享代码替代归档")
    return candidate, url, commit.lower(), license_value, _audit_evidence(audit)


def _audit_v1_repositories(
    repositories: Sequence[Mapping[str, Any]],
    *,
    code_audit: Mapping[str, dict[str, Any]],
    unit_root: Path,
    core_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str], list[str]]:
    corrected: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for legacy in repositories:
        paper_id = str(legacy.get("paper_id") or "").strip()
        legacy_status = str(legacy.get("status") or "unknown").lower()
        if paper_id == "liteframe-2026":
            corrected.append(
                {
                    "paper_id": paper_id,
                    "status": "announced_pending",
                    "url": _sanitize_url(legacy.get("url")),
                    "path": None,
                    "commit": None,
                    "license": None,
                    "source": "v1-audit-correction",
                    "note": (
                        "v1 目录只有 README/assets，README 明示 code soon；"
                        "不得计为公开可用官方实现。"
                    ),
                    "historical_v1_path": legacy.get("path"),
                }
            )
            warnings.append("vlm_v1_liteframe_corrected_to_announced_pending")
            continue
        if legacy_status in {"none_found", "announced_pending"}:
            corrected.append(
                {
                    "paper_id": paper_id,
                    "status": legacy_status,
                    "url": _sanitize_url(legacy.get("url")),
                    "path": None,
                    "commit": None,
                    "license": None,
                    "source": "vlm-v1-verified-status",
                    "note": legacy.get("note"),
                }
            )
            continue
        if legacy_status != "valid":
            blockers.append(f"vlm_v1_repository_status_unknown:{paper_id}:{legacy_status}")
            corrected.append(
                {
                    "paper_id": paper_id,
                    "status": "blocked",
                    "source": "vlm-v1-audit",
                    "note": f"unknown legacy status: {legacy_status}",
                }
            )
            continue
        expected_url = _sanitize_url(legacy.get("url"))
        expected_commit = str(legacy.get("commit") or "").lower()
        path_value = legacy.get("path")
        if not expected_url or not COMMIT_RE.fullmatch(expected_commit) or not path_value:
            blockers.append(f"vlm_v1_repository_contract_invalid:{paper_id}")
            corrected.append(
                {
                    "paper_id": paper_id,
                    "status": "blocked",
                    "source": "vlm-v1-audit",
                    "note": "legacy URL/path/commit contract invalid",
                }
            )
            continue
        legacy_path = unit_root / Path(str(path_value))
        inspection = _inspect_repository(
            legacy_path,
            core_root=core_root,
            expected_url=expected_url,
            expected_commit=expected_commit,
        )
        selected_path = legacy_path
        selected_url = expected_url
        selected_commit = expected_commit
        selected_license = legacy.get("license")
        selected_evidence: list[str] = []
        source = "vlm-v1-clean-fixed-commit"
        if not inspection["ready"]:
            replacement = _baseline_replacement_candidate(
                code_audit.get(paper_id),
                unit_root=unit_root,
                core_root=core_root,
            )
            replacement_inspection: dict[str, Any] | None = None
            if replacement:
                candidate_path, candidate_url, candidate_commit, candidate_license, evidence = (
                    replacement
                )
                if _canonical_repository_key(candidate_url) == _canonical_repository_key(
                    expected_url
                ):
                    replacement_inspection = _inspect_repository(
                        candidate_path,
                        core_root=core_root,
                        expected_url=expected_url,
                        expected_commit=candidate_commit,
                    )
                    if replacement_inspection["ready"]:
                        selected_path = candidate_path
                        selected_url = candidate_url
                        selected_commit = candidate_commit
                        selected_license = candidate_license
                        selected_evidence = evidence
                        inspection = replacement_inspection
                        source = "shared-clean-fixed-commit-replacement"
            if not inspection["ready"]:
                detail = ",".join(str(value) for value in inspection.get("errors", []))
                blockers.append(f"vlm_v1_repository_unusable:{paper_id}:{detail}")
                corrected.append(
                    {
                        "paper_id": paper_id,
                        "status": "blocked",
                        "url": expected_url,
                        "path": str(path_value),
                        "commit": expected_commit,
                        "source": "vlm-v1-audit",
                        "inspection": inspection,
                        "note": "旧仓库不干净且没有同 remote 的干净固定 commit 共享归档。",
                    }
                )
                continue
        corrected.append(
            {
                "paper_id": paper_id,
                "status": "official_local",
                "url": selected_url,
                "path": os.path.relpath(selected_path, start=unit_root).replace("\\", "/"),
                "commit": selected_commit,
                "license": selected_license,
                "source": source,
                "inspection": inspection,
                "evidence": selected_evidence,
                "historical_v1_path": legacy.get("path"),
            }
        )
    counts = {
        "total": len(corrected),
        "official_local": sum(1 for item in corrected if item.get("status") == "official_local"),
        "announced_pending": sum(
            1 for item in corrected if item.get("status") == "announced_pending"
        ),
        "none_found": sum(1 for item in corrected if item.get("status") == "none_found"),
        "blocked": sum(1 for item in corrected if item.get("status") == "blocked"),
    }
    return corrected, counts, blockers, warnings


def _sanitize_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.query:
        return None
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{port}{path}"


def _audit_evidence(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    reason = item.get("evidence_reason")
    if isinstance(reason, str) and reason.strip():
        values.append(reason.strip())
    for key in ("evidence", "evidence_urls"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
        elif isinstance(raw, list):
            values.extend(str(value).strip() for value in raw if str(value).strip())
    no_code = item.get("no_code_review")
    if isinstance(no_code, Mapping):
        exact_title = no_code.get("exact_title")
        reviewed_at = no_code.get("reviewed_at")
        if isinstance(exact_title, str) and exact_title.strip():
            values.append(f"reviewed no-code title: {exact_title.strip()}")
        if isinstance(reviewed_at, str) and reviewed_at.strip():
            values.append(f"reviewed at: {reviewed_at.strip()}")
        nested_urls = no_code.get("evidence_urls")
        if isinstance(nested_urls, list):
            values.extend(str(value).strip() for value in nested_urls if str(value).strip())
    return list(dict.fromkeys(values))


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_code(
    record: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
    *,
    unit_root: Path,
    core_root: Path,
) -> tuple[dict[str, Any], list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    key = _record_key(record)
    if audit is None:
        return (
            {
                "status": "unknown",
                "url": None,
                "local_path": None,
                "commit": None,
                "license": None,
                "evidence": [],
            },
            [f"code_audit_missing:{key}"],
            warnings,
        )
    raw_status = str(audit.get("status") or "unknown").strip().lower()
    selected = audit.get("selected_local_archive")
    selected = selected if isinstance(selected, dict) else {}
    url = _sanitize_url(
        _first_string(
            audit.get("official_url"),
            selected.get("url"),
            (audit.get("official_urls") or [None])[0]
            if isinstance(audit.get("official_urls"), list)
            else None,
            (record.get("code") or {}).get("url") if isinstance(record.get("code"), dict) else None,
        )
    )
    local_value = _first_string(
        audit.get("local_path"),
        audit.get("archive_path"),
        selected.get("path"),
        selected.get("local_path"),
        (record.get("code") or {}).get("local_path")
        if isinstance(record.get("code"), dict)
        else None,
    )
    commit = _first_string(
        audit.get("commit"),
        selected.get("commit"),
        (record.get("code") or {}).get("commit") if isinstance(record.get("code"), dict) else None,
    )
    license_value = _first_string(
        audit.get("license"),
        selected.get("license"),
        (record.get("code") or {}).get("license") if isinstance(record.get("code"), dict) else None,
    )
    evidence = _audit_evidence(audit)
    record_code = record.get("code")
    if isinstance(record_code, Mapping):
        evidence = list(dict.fromkeys([*evidence, *_audit_evidence(record_code)]))
    local_path: Path | None = None
    local_rel: str | None = None
    if local_value:
        candidate = Path(local_value)
        local_path = candidate if candidate.is_absolute() else unit_root / candidate
        local_path = _ensure_inside(local_path, core_root, f"代码目录 {key}")
        local_rel = os.path.relpath(local_path, start=unit_root).replace("\\", "/")

    officialish = raw_status in {"official_local", "official_public", "official_url_only"}
    is_local = bool(
        local_path and local_path.is_dir() and commit and COMMIT_RE.fullmatch(commit.lower())
    )
    if officialish and is_local:
        status = "official_local"
    elif officialish:
        status = "official_url_only"
        blockers.append(f"official_code_not_archived:{key}")
    elif raw_status in {"none_found", "not_provided"}:
        status = "none_found"
        if not evidence:
            blockers.append(f"no_code_evidence_missing:{key}")
    elif raw_status == "third_party_only":
        status = "third_party_only"
        if not evidence:
            blockers.append(f"third_party_evidence_missing:{key}")
    elif raw_status == "announced_pending":
        status = "announced_pending"
    elif raw_status == "not_applicable":
        status = "not_applicable"
    else:
        status = "unknown"
        blockers.append(f"code_audit_unresolved:{key}:{raw_status}")
    if url is None and officialish:
        blockers.append(f"official_code_url_missing_or_unsafe:{key}")
    return (
        {
            "status": status,
            "url": url,
            "local_path": local_rel,
            "commit": commit.lower() if commit else None,
            "license": license_value,
            "evidence": evidence,
            "audit_status": raw_status,
            "reviewed_at": audit.get("reviewed_at"),
        },
        blockers,
        warnings,
    )


def _evidence_items(evidence: Mapping[str, Any], category: str) -> list[dict[str, Any]]:
    groups = evidence.get("evidence")
    values = groups.get(category) if isinstance(groups, dict) else None
    return (
        [dict(item) for item in values if isinstance(item, dict)]
        if isinstance(values, list)
        else []
    )


def _evidence_line(item: Mapping[str, Any]) -> str:
    excerpt = str(item.get("excerpt") or "").replace("\ufffd", "[原文字符未识别]")
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:277].rstrip() + "…"
    evidence_id = item.get("evidence_id") or "未编号"
    page = item.get("page") or "?"
    section = str(item.get("section") or "未识别章节").replace(
        "\ufffd", "[原文字符未识别]"
    )
    return f"证据 {evidence_id}，p.{page}，{section}：{excerpt or '待核验'}"


def _analysis_for_html(
    record: Mapping[str, Any], evidence: Mapping[str, Any], code: Mapping[str, Any]
) -> dict[str, Any]:
    analysis_value = record.get("analysis")
    original: Mapping[str, Any] = analysis_value if isinstance(analysis_value, dict) else {}
    mapping = {
        "problem": ("abstract", "introduction_contributions"),
        "method": ("method",),
        "results": ("experiments_results",),
        "limitations": ("limitations_conclusion",),
    }
    result: dict[str, Any] = {}
    for field, categories in mapping.items():
        existing = original.get(field)
        if existing:
            result[field] = existing
            continue
        lines: list[str] = []
        for category in categories:
            lines.extend(_evidence_line(item) for item in _evidence_items(evidence, category))
        result[field] = lines or None
    relationships = original.get("relationships")
    if not relationships:
        version = record.get("version_relation")
        relationships = (
            ["身份/版本关系见本单元 source-record catalog；该关系来自显式去重决策。"]
            if version
            else None
        )
    result["relationships"] = relationships
    reproduction = original.get("reproduction")
    if not reproduction:
        reproduction = [
            f"代码审计状态：{code.get('status', 'unknown')}。",
            "复现实验参数与数据许可必须回到论文及固定 commit 再核验。",
        ]
    result["reproduction"] = reproduction
    return result


def _analysis_markdown(
    record: Mapping[str, Any], evidence: Mapping[str, Any], code: Mapping[str, Any]
) -> str:
    title = str(record.get("title") or record.get("paper_id") or "待核验")
    lines = [
        f"# {title}",
        "",
        (
            "> 本文件是原文证据定位页，不把机械抽取片段冒充人工结论。"
            "中文综合解读尚未核验的部分明确保留为“待核验”。"
        ),
        "",
    ]
    sections = [
        ("摘要与问题", ("abstract", "introduction_contributions")),
        ("方法证据", ("method",)),
        ("实验与结果证据", ("experiments_results",)),
        ("局限与结论证据", ("limitations_conclusion",)),
        ("代码线索（仅候选）", ("code_urls",)),
    ]
    for heading, categories in sections:
        lines.extend([f"## {heading}", ""])
        items: list[dict[str, Any]] = []
        for category in categories:
            items.extend(_evidence_items(evidence, category))
        if items:
            lines.extend(f"- {_evidence_line(item)}" for item in items)
        else:
            lines.append("- 待核验")
        lines.append("")
    lines.extend(
        [
            "## 代码审计",
            "",
            f"- 状态：`{code.get('status', 'unknown')}`",
            f"- 官方 URL：{code.get('url') or '待核验'}",
            f"- 本地固定 commit：{code.get('commit') or '待核验'}",
        ]
    )
    for item in code.get("evidence") or []:
        lines.append(f"- 审计依据：{item}")
    lines.extend(["", "## 写作状态", "", "- 人工中文综合：待核验", ""])
    return "\n".join(lines)


def _html_rel_for_record(
    domain: str,
    record: Mapping[str, Any],
    *,
    baseline_ids: set[str],
) -> str:
    source_id = _record_key(record)
    canonical_id = str(record.get("canonical_paper_id") or _paper_id(record))
    relation_value = record.get("version_relation")
    relation: Mapping[str, Any] = relation_value if isinstance(relation_value, dict) else {}
    relation_type = relation.get("relation_type")
    if domain == "video-llm-compression" and relation_type == "alternate_version_of":
        return f"library/site/versions/{canonical_id}/{source_id}.html"
    if domain == "video-llm-compression" and canonical_id in baseline_ids:
        return f"library/site/source-records/{source_id}.html"
    return f"library/site/papers/{source_id}.html"


def _validate_record_basics(record: Mapping[str, Any], domain: str) -> list[str]:
    blockers: list[str] = []
    source_id = _record_key(record)
    paper_id = _paper_id(record)
    if not source_id or not SAFE_ID_RE.fullmatch(source_id):
        blockers.append(f"source_record_id_invalid:{source_id or '<missing>'}")
    if not paper_id or not SAFE_ID_RE.fullmatch(paper_id):
        blockers.append(f"paper_id_invalid:{source_id}:{paper_id or '<missing>'}")
    domains = record.get("domain_slugs")
    if not isinstance(domains, list) or domain not in domains:
        blockers.append(f"domain_membership_missing:{source_id}:{domain}")
    if not isinstance(record.get("title"), str) or not str(record.get("title")).strip():
        blockers.append(f"title_missing:{source_id}")
    pdf = record.get("pdf")
    if not isinstance(pdf, dict) or not SHA256_RE.fullmatch(str(pdf.get("sha256") or "")):
        blockers.append(f"pdf_sha256_invalid:{source_id}")
    return blockers


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _canonical_catalog(
    domain: str,
    source_records: list[dict[str, Any]],
    baseline_papers: list[dict[str, Any]],
    recovered_versions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in source_records:
        grouped[str(record["canonical_paper_id"])].append(record)
    result: list[dict[str, Any]] = []
    baseline_by_id = {str(item.get("id")): item for item in baseline_papers}
    recovered_by_id = {str(item.get("canonical_paper_id")): item for item in recovered_versions}
    for canonical_id in sorted(set(baseline_by_id) | set(grouped)):
        sources = sorted(grouped.get(canonical_id, []), key=lambda item: item["source_record_id"])
        baseline = baseline_by_id.get(canonical_id)
        if baseline:
            title = baseline.get("title")
            year = baseline.get("year")
            venue = baseline.get("venue")
            primary_html = (
                baseline.get("analysis_html") or f"library/site/papers/{canonical_id}.html"
            )
            primary_pdf = baseline.get("pdf_local") or f"library/papers/{canonical_id}/paper.pdf"
        else:
            primary = sources[0]
            title = primary.get("title")
            year = primary.get("year")
            venue = primary.get("venue")
            primary_html = primary.get("html_path")
            primary_pdf = primary.get("pdf", {}).get("path")
        recovered = recovered_by_id.get(canonical_id)
        aliases: list[str] = []
        if recovered:
            title = recovered.get("canonical_title") or title
            primary_html = recovered.get("html_path") or primary_html
            primary_pdf = (recovered.get("pdf") or {}).get("path") or primary_pdf
            alternate_title = recovered.get("alternate_title")
            if alternate_title:
                aliases.append(str(alternate_title))
        result.append(
            {
                "canonical_paper_id": canonical_id,
                "title": title,
                "year": year,
                "venue": venue,
                "provenance": ["vlm-v1"] if baseline else ["desktop-intake-2026-08-31"],
                "source_record_ids": [item["source_record_id"] for item in sources],
                "primary_pdf_path": primary_pdf,
                "primary_html_path": primary_html,
                "aliases": aliases,
                "recovered_versions": [recovered] if recovered else [],
                "versions": [
                    {
                        "source_record_id": item["source_record_id"],
                        "relation_type": (item.get("version_relation") or {}).get("relation_type"),
                        "pdf_path": item["pdf"]["path"],
                        "html_path": item["html_path"],
                    }
                    for item in sources
                ],
                "legacy_v1": baseline,
            }
        )
    return result


def _index_records(
    canonical: Sequence[Mapping[str, Any]],
    source_records: Sequence[Mapping[str, Any]],
    baseline_repositories: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    source_by_id = {str(item.get("source_record_id")): item for item in source_records}
    repo_by_id = {str(item.get("paper_id")): item for item in baseline_repositories}
    rows: list[dict[str, Any]] = []
    for canonical_item in canonical:
        legacy = canonical_item.get("legacy_v1")
        if isinstance(legacy, dict):
            canonical_id = str(canonical_item.get("canonical_paper_id"))
            repository = repo_by_id.get(canonical_id, {})
            raw_status = str(repository.get("status") or "unknown")
            status = {
                "valid": "official_local",
                "official_local": "official_local",
                "none_found": "none_found",
                "announced_pending": "announced_pending",
                "blocked": "unknown",
            }.get(raw_status, "unknown")
            rows.append(
                {
                    "title": canonical_item.get("title"),
                    "authors": legacy.get("authors"),
                    "year": canonical_item.get("year"),
                    "venue": canonical_item.get("venue"),
                    "html_path": canonical_item.get("primary_html_path"),
                    "pdf": {"path": canonical_item.get("primary_pdf_path")},
                    "code": {"status": status},
                }
            )
            continue
        source_ids = canonical_item.get("source_record_ids") or []
        source = next(
            (
                source_by_id.get(str(source_id))
                for source_id in source_ids
                if source_by_id.get(str(source_id))
            ),
            None,
        )
        if source:
            rows.append(dict(source))
    return rows


def _render_index(
    *,
    domain: str,
    label: str,
    snapshot_date: str,
    counts: Mapping[str, int],
    records: Sequence[Mapping[str, Any]],
) -> str:
    rows: list[str] = []
    index_parent = Path(DOMAIN_CONFIG[domain]["site_index"]).parent
    for record in sorted(
        records, key=lambda item: (-(int(item.get("year") or 0)), str(item.get("title") or ""))
    ):
        title = html.escape(str(record.get("title") or "待核验"))
        authors = record.get("authors")
        if isinstance(authors, list):
            author_text = ", ".join(str(value) for value in authors)
        else:
            author_text = str(authors or "待核验")
        page = html.escape(
            os.path.relpath(
                Path(str(record.get("index_html_path") or record["html_path"])),
                start=index_parent,
            ).replace("\\", "/")
        )
        pdf = html.escape(
            os.path.relpath(
                Path(str(record.get("index_pdf_path") or record["pdf"]["path"])),
                start=index_parent,
            ).replace("\\", "/")
        )
        code = html.escape(str((record.get("code") or {}).get("status") or "unknown"))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(record.get('year') or '待核验'))}</td>"
            f'<td><a href="{page}">{title}</a><br><small>{html.escape(author_text)}</small></td>'
            f"<td>{html.escape(str(record.get('venue') or '待核验'))}</td>"
            f'<td><span class="badge">{code}</span></td>'
            f'<td><a href="{pdf}">PDF</a></td>'
            "</tr>"
        )
    stats = "".join(
        f'<div class="stat"><strong>{value}</strong>{html.escape(key.replace("_", " "))}</div>'
        for key, value in counts.items()
        if key
        in {
            "canonical_papers",
            "source_records",
            "exact_reuse",
            "alternate_versions",
            "recovered_versions",
            "version_pages",
        }
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(label)} · v2</title>
<link rel="stylesheet" href="assets/compression-unit-v2.css"></head>
<body><main><p><a href="../../{DOMAIN_CONFIG[domain]["readme"]}">README</a></p>
<h1>{html.escape(label)}</h1>
<p class="meta">v2 规范目录 · 快照 {html.escape(snapshot_date)} · 新数据不生成 guide 表示。</p>
<section class="summary">{stats}</section><div class="scroll"><table><thead><tr>
<th>年份</th><th>论文</th><th>场所</th><th>代码</th><th>资料</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div></main></body></html>
"""


def _render_root_entry(domain: str) -> str:
    target = DOMAIN_CONFIG[domain]["site_index"]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0;url={html.escape(target, quote=True)}">
<title>打开资料库</title></head>
<body><p><a href="{html.escape(target, quote=True)}">打开压缩论文资料库 v2</a></p></body></html>
"""


def _render_readme(domain: str, counts: Mapping[str, int], snapshot_date: str) -> str:
    config = DOMAIN_CONFIG[domain]
    manifest = config["manifest"]
    validator = config["validator"]
    return f"""# {config["label"]} · 规范资料单元 v2

本目录由 `compression_unit_builder` 从已核验 paper-record、逐篇原文证据包和
官方代码审计确定性生成。快照日期：{snapshot_date}。

- 规范论文：{counts["canonical_papers"]}
- 本次来源记录：{counts["source_records"]}
- 同字节历史复用：{counts["exact_reuse"]}
- 替代 PDF 版本：{counts["alternate_versions"]}
- 显式恢复版本：{counts["recovered_versions"]}
- 独立版本页：{counts["version_pages"]}
- 新数据 **不生成已废除的 guide 表示**；逐篇 HTML 是正式 HTML 表示。

推荐入口：[`{config["entry"]}`]({config["entry"]})。论文目录与版本关系见
`library/catalog/papers.v2.json`，代码结论见
`library/catalog/repositories.v2.json`，构建清单见 `{manifest}`。

验证：

```powershell
python ./{validator} --unit-root . --manifest ./{manifest}
```

VLM v1 的 33 篇论文、原 PDF、分析、HTML、代码目录和 `*.v1.json`
均为历史只读证据；v2 只新增独立命名的目录、目录文件和页面，不覆盖 v1。
"""


def _protected_v1_paths(unit_root: Path) -> set[str]:
    protected: set[str] = set()
    if not unit_root.exists():
        return protected
    baseline_ids: set[str] = set()
    baseline_catalog = unit_root / "library/catalog/papers.v1.json"
    if baseline_catalog.is_file():
        try:
            payload = json.loads(baseline_catalog.read_text(encoding="utf-8"))
            baseline_ids = {
                str(item.get("id"))
                for item in payload.get("papers", [])
                if isinstance(item, dict) and item.get("id")
            }
        except (OSError, UnicodeError, json.JSONDecodeError):
            # _load_v1 reports the actionable error before apply; protection remains conservative.
            baseline_ids = set()
    for path in unit_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(unit_root).as_posix()
        baseline_owned = any(
            rel.startswith(f"library/papers/{paper_id}/")
            or rel.startswith(f"library/code/upstream/{paper_id}/")
            or rel.startswith(f"library/code/guides/{paper_id}/")
            or rel == f"library/analysis/{paper_id}.md"
            or rel == f"library/site/papers/{paper_id}.html"
            for paper_id in baseline_ids
        )
        if (
            rel in {"README.md", "index.html", "outputs/manifest.json", "library/site/index.html"}
            or ("/catalog/" in f"/{rel}" and ".v1." in rel)
            or baseline_owned
            or (rel.startswith("scripts/") and rel != "scripts/validate_unit_v2.py")
        ):
            protected.add(rel)
    return protected


def _invoke_html_generator(
    records_by_parent: Mapping[Path, list[dict[str, Any]]],
    *,
    staging_root: Path,
    core_root: Path,
    domain: str,
) -> list[str]:
    generated: list[str] = []
    input_root = staging_root / ".html-inputs" / domain
    report_root = staging_root / ".html-reports" / domain
    for index, (output_parent, records) in enumerate(
        sorted(records_by_parent.items(), key=lambda item: str(item[0]))
    ):
        input_path = input_root / f"batch-{index:03d}.json"
        report_path = report_root / f"batch-{index:03d}.json"
        _write_json(input_path, {"records": records})
        command = [
            sys.executable,
            str(HTML_TOOL_DIR / "generate.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_parent),
            "--allowed-root",
            str(core_root),
            "--report",
            str(report_path),
            "--skip-local-link-check",
        ]
        completed = subprocess.run(
            command,
            cwd=HUB_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(120, len(records) * 8),
            check=False,
        )
        if completed.returncode != 0:
            raise BuildError(
                f"paper_intake_html 生成失败（{domain}, batch {index}）："
                f"{completed.stderr[-2000:]}\n{completed.stdout[-2000:]}"
            )
        for record in records:
            generated.append((output_parent / f"{record['paper_id']}.html").as_posix())
    return generated


def _strict_validate_generated_html(paths: Iterable[Path], core_root: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "compression_unit_builder_paper_html", HTML_TOOL_DIR / "paper_html.py"
    )
    if spec is None or spec.loader is None:
        raise BuildError("无法载入 paper_intake_html 验证器。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    errors: list[str] = []
    for path in paths:
        result = module.validate_html_file(path, check_local_links=True, allowed_root=core_root)
        errors.extend(f"{path}:{issue.code}:{issue.message}" for issue in result.errors)
    return errors


def _validate_expected(
    actual: Mapping[str, int], expected: Mapping[str, int], domain: str
) -> list[str]:
    return [
        f"count_mismatch:{domain}:{key}:{actual.get(key)}!={value}"
        for key, value in expected.items()
        if actual.get(key) != value
    ]


def _build_one(
    *,
    domain: str,
    records: list[dict[str, Any]],
    evidence_by_key: Mapping[str, dict[str, Any]],
    code_audit: Mapping[str, dict[str, Any]],
    version_recoveries: Mapping[str, dict[str, Any]],
    core_root: Path,
    staging_root: Path,
    snapshot_date: str,
    build_id: str,
    expected: Mapping[str, int],
    input_digests: Mapping[str, str],
) -> UnitBuild:
    config = DOMAIN_CONFIG[domain]
    unit_root = _ensure_inside(core_root / config["directory"], core_root, f"{domain} 单元")
    unit_stage = staging_root / "units" / domain
    if unit_stage.exists():
        raise BuildError(f"staging 单元目录已存在：{unit_stage}")
    unit_stage.mkdir(parents=True)
    blockers: list[str] = []
    warnings: list[str] = []
    baseline_papers: list[dict[str, Any]] = []
    baseline_repositories: list[dict[str, Any]] = []
    legacy_baseline_repositories: list[dict[str, Any]] = []
    baseline_repository_counts = {
        "total": 0,
        "official_local": 0,
        "announced_pending": 0,
        "none_found": 0,
        "blocked": 0,
    }
    if domain == "video-llm-compression":
        baseline_papers, legacy_baseline_repositories = _load_v1(unit_root)
        (
            baseline_repositories,
            baseline_repository_counts,
            baseline_repository_blockers,
            baseline_repository_warnings,
        ) = _audit_v1_repositories(
            legacy_baseline_repositories,
            code_audit=code_audit,
            unit_root=unit_root,
            core_root=core_root,
        )
        blockers.extend(baseline_repository_blockers)
        warnings.extend(baseline_repository_warnings)
    baseline_ids = {str(item.get("id")) for item in baseline_papers}
    source_ids = [_record_key(record) for record in records]
    if len(source_ids) != len(set(source_ids)):
        duplicates = sorted({value for value in source_ids if source_ids.count(value) > 1})
        blockers.extend(f"duplicate_source_record_id:{value}" for value in duplicates)

    normalized: list[dict[str, Any]] = []
    recovered_normalized: list[dict[str, Any]] = []
    html_batches: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    exact_reuse = 0
    alternate_versions = 0
    matched_recoveries: set[str] = set()
    for raw in sorted(records, key=_record_key):
        blockers.extend(_validate_record_basics(raw, domain))
        source_id = _record_key(raw)
        paper_id = _paper_id(raw)
        canonical_id = str(raw.get("canonical_paper_id") or paper_id)
        if not SAFE_ID_RE.fullmatch(canonical_id):
            blockers.append(f"canonical_paper_id_invalid:{source_id}:{canonical_id}")
        relation = (
            raw.get("version_relation") if isinstance(raw.get("version_relation"), dict) else None
        )
        relation_type = relation.get("relation_type") if relation else None
        recovery = version_recoveries.get(source_id) or version_recoveries.get(paper_id)
        if recovery:
            matched_recoveries.add(str(recovery.get("paper_id") or source_id))
        if recovery and domain != "video-llm-compression":
            blockers.append(f"version_recovery_outside_video_domain:{source_id}")
            recovery = None
        if relation_type == "same_representation_as":
            exact_reuse += 1
        elif relation_type == "alternate_version_of":
            alternate_versions += 1
        if relation_type in {"same_representation_as", "alternate_version_of"}:
            if domain != "video-llm-compression" or canonical_id not in baseline_ids:
                blockers.append(f"version_relation_without_v1_target:{source_id}:{canonical_id}")
        evidence = evidence_by_key.get(source_id) or evidence_by_key.get(paper_id)
        if evidence is None:
            blockers.append(f"analysis_evidence_missing:{source_id}")
            evidence = {
                "schema_version": "paper-analysis-evidence.v1",
                "paper_id": paper_id,
                "status": "error",
                "evidence": {},
                "missing_categories": [
                    "abstract",
                    "introduction_contributions",
                    "method",
                    "experiments_results",
                    "limitations_conclusion",
                    "code_urls",
                ],
                "warnings": ["evidence package missing"],
            }
        elif str(evidence.get("status") or "success").lower() == "error":
            blockers.append(f"analysis_evidence_error:{source_id}")
        audit = code_audit.get(source_id) or code_audit.get(paper_id)
        code, code_blockers, code_warnings = _normalize_code(
            raw, audit, unit_root=unit_root, core_root=core_root
        )
        blockers.extend(code_blockers)
        warnings.extend(code_warnings)

        target_pdf_value = raw.get("target_pdf_path")
        if not isinstance(target_pdf_value, str) or not target_pdf_value.strip():
            blockers.append(f"target_pdf_path_missing:{source_id}")
            target_pdf = unit_root / "library/papers" / canonical_id / "paper.pdf"
        else:
            target_pdf = Path(target_pdf_value)
        target_pdf = _ensure_inside(target_pdf, unit_root, f"PDF {source_id}")
        pdf_rel = target_pdf.relative_to(unit_root).as_posix()
        if relation_type == "alternate_version_of":
            required_versions = (unit_root / "library/papers" / canonical_id / "versions").resolve(
                strict=False
            )
            if not _inside(target_pdf, required_versions):
                blockers.append(f"alternate_pdf_outside_versions:{source_id}:{pdf_rel}")

        html_rel = _html_rel_for_record(domain, raw, baseline_ids=baseline_ids)
        if recovery:
            html_rel = f"library/site/versions/{canonical_id}/legacy/{source_id}.html"
        final_html = unit_root / Path(html_rel)
        stage_html = unit_stage / Path(html_rel)
        analysis_rel = f"{config['analysis_root']}/{source_id}.md"
        evidence_rel = f"{config['evidence_root']}/{source_id}.evidence.json"
        _write_bytes(
            unit_stage / analysis_rel, _analysis_markdown(raw, evidence, code).encode("utf-8")
        )
        evidence_copy = dict(evidence)
        evidence_copy.setdefault("paper_id", paper_id)
        evidence_copy["source_record_id"] = source_id
        _write_json(unit_stage / evidence_rel, evidence_copy)

        html_code = dict(code)
        if code.get("local_path"):
            code_target = unit_root / Path(str(code["local_path"]))
            html_code["local_path"] = _relative_link(code_target, final_html)
        html_record = dict(raw)
        html_record["paper_id"] = paper_id
        if recovery:
            html_record["title"] = recovery.get("alternate_title") or raw.get("title")
        html_record["pdf"] = dict(raw.get("pdf") or {})
        html_record["pdf"]["path"] = _relative_link(target_pdf, final_html)
        html_record["analysis"] = _analysis_for_html(raw, evidence, code)
        html_record["code"] = html_code
        html_record["warnings"] = list(raw.get("warnings") or []) + list(
            evidence.get("warnings") or []
        )
        if recovery:
            recovery_evidence_preview = [
                str(item)
                for item in recovery.get("evidence", [])
                if isinstance(item, str) and item.strip()
            ]
            html_record["analysis"]["relationships"] = [
                "该 PDF 是 canonical 论文的旧题名版本；当前题名 PDF 另有独立页面。",
                *recovery_evidence_preview,
            ]
            html_record["decision_evidence"] = list(
                dict.fromkeys(
                    [
                        *list(raw.get("decision_evidence") or []),
                        *recovery_evidence_preview,
                    ]
                )
            )
            html_record["warnings"] = list(
                dict.fromkeys(
                    [
                        *html_record["warnings"],
                        "当前页面保留旧题名 PDF；canonical 题名与代码归属以恢复报告为准。",
                    ]
                )
            )
        html_batches[stage_html.parent].append(html_record)

        normalized_record: dict[str, Any] = {
            "source_record_id": source_id,
            "paper_id": paper_id,
            "canonical_paper_id": canonical_id,
            "title": raw.get("title"),
            "title_zh": raw.get("title_zh"),
            "authors": raw.get("authors"),
            "year": raw.get("year"),
            "venue": raw.get("venue"),
            "doi": raw.get("doi"),
            "arxiv_id": raw.get("arxiv_id"),
            "tags": raw.get("tags") or [],
            "domain_slugs": raw.get("domain_slugs") or [],
            "source_urls": raw.get("source_urls") or [],
            "pdf": {
                **dict(raw.get("pdf") or {}),
                "path": pdf_rel,
            },
            "html_path": html_rel,
            "analysis_path": analysis_rel,
            "evidence_path": evidence_rel,
            "code": code,
            "version_relation": relation,
            "decision_evidence": raw.get("decision_evidence") or [],
            "warnings": raw.get("warnings") or [],
        }

        if recovery:
            canonical_title = str(recovery.get("canonical_title") or "").strip()
            alternate_title = str(recovery.get("alternate_title") or "").strip()
            identity_decision = str(recovery.get("identity_decision") or "").strip()
            recovery_evidence = recovery.get("evidence")
            if not canonical_title or not alternate_title or not identity_decision:
                blockers.append(f"version_recovery_identity_fields_missing:{source_id}")
            if canonical_title and canonical_title != str(raw.get("title") or "").strip():
                blockers.append(f"version_recovery_canonical_title_conflict:{source_id}")
            audited_title = _first_string(
                audit.get("exact_title") if audit else None,
                audit.get("title") if audit else None,
            )
            if audited_title and canonical_title and audited_title != canonical_title:
                blockers.append(f"version_recovery_code_title_conflict:{source_id}")
            if not isinstance(recovery_evidence, list) or not all(
                isinstance(item, str) and item.strip() for item in recovery_evidence
            ):
                blockers.append(f"version_recovery_evidence_missing:{source_id}")
                recovery_evidence = []
            recovery_path_value = recovery.get("local_path")
            if not isinstance(recovery_path_value, str) or not recovery_path_value.strip():
                blockers.append(f"version_recovery_local_path_missing:{source_id}")
                recovery_path = unit_root / "library/papers" / canonical_id / "versions/missing.pdf"
            else:
                recovery_path = Path(recovery_path_value)
            recovery_path = _ensure_inside(recovery_path, unit_root, f"恢复版本 PDF {source_id}")
            required_versions = (unit_root / "library/papers" / canonical_id / "versions").resolve(
                strict=False
            )
            if not _inside(recovery_path, required_versions):
                blockers.append(f"version_recovery_pdf_outside_versions:{source_id}")
            recovery_sha = str(recovery.get("sha256") or "").lower()
            recovery_size = recovery.get("bytes")
            recovery_pages = recovery.get("pages")
            if not SHA256_RE.fullmatch(recovery_sha):
                blockers.append(f"version_recovery_sha256_invalid:{source_id}")
            if not isinstance(recovery_size, int) or recovery_size < 1:
                blockers.append(f"version_recovery_size_invalid:{source_id}")
            if not isinstance(recovery_pages, int) or recovery_pages < 1:
                blockers.append(f"version_recovery_pages_invalid:{source_id}")
            arxiv_id = str(recovery.get("arxiv_id") or "").strip()
            version_suffix = re.sub(r"[^a-z0-9]+", "-", arxiv_id.lower()).strip("-")
            version_id = f"{source_id}-arxiv-{version_suffix or 'recovered'}"
            recovered_html_rel = f"library/site/papers/{source_id}.html"
            recovered_final_html = unit_root / recovered_html_rel
            recovered_stage_html = unit_stage / recovered_html_rel
            recovered_html_record = dict(html_record)
            recovered_analysis = dict(html_record["analysis"])
            recovered_analysis["relationships"] = [
                "该 PDF 是当前 canonical 题名版本；旧题名 PDF 作为 alternate 独立保留。",
                *list(recovery_evidence),
            ]
            recovered_html_record["analysis"] = recovered_analysis
            recovered_html_code = dict(code)
            if code.get("local_path"):
                recovered_code_target = unit_root / Path(str(code["local_path"]))
                recovered_html_code["local_path"] = _relative_link(
                    recovered_code_target, recovered_final_html
                )
            recovered_html_record["code"] = recovered_html_code
            recovered_html_record["title"] = canonical_title or raw.get("title")
            recovered_html_record["arxiv_id"] = arxiv_id or raw.get("arxiv_id")
            recovered_urls = list(raw.get("source_urls") or [])
            source_url = _sanitize_url(recovery.get("source_url"))
            if source_url:
                recovered_urls.append({"label": "recovered canonical version", "url": source_url})
            recovered_html_record["source_urls"] = recovered_urls
            recovered_html_record["pdf"] = {
                "path": _relative_link(recovery_path, recovered_final_html),
                "sha256": recovery_sha,
                "size": recovery_size,
                "page_count": recovery_pages,
                "version": (f"arXiv {arxiv_id}" if arxiv_id else "recovered canonical version"),
            }
            recovered_html_record["warnings"] = list(
                dict.fromkeys(
                    [
                        *list(recovered_html_record.get("warnings") or []),
                        "该页对应显式恢复的 canonical PDF；旧题名 PDF 另有独立版本页。",
                    ]
                )
            )
            html_batches[recovered_stage_html.parent].append(recovered_html_record)
            recovered_entry: dict[str, Any] = {
                "version_id": version_id,
                "paper_id": paper_id,
                "source_record_id": source_id,
                "canonical_paper_id": canonical_id,
                "canonical_title": canonical_title,
                "alternate_title": alternate_title,
                "identity_decision": identity_decision,
                "identity_evidence": list(recovery_evidence),
                "arxiv_id": arxiv_id or None,
                "source_url": source_url,
                "pdf": {
                    "path": recovery_path.relative_to(unit_root).as_posix(),
                    "sha256": recovery_sha,
                    "size": recovery_size,
                    "page_count": recovery_pages,
                    "version": (f"arXiv {arxiv_id}" if arxiv_id else "recovered canonical version"),
                },
                "html_path": recovered_html_rel,
                "analysis_path": analysis_rel,
                "evidence_path": evidence_rel,
                "code": code,
                "relation_type": "canonical_title_revision_of",
            }
            recovered_normalized.append(recovered_entry)
            normalized_record["aliases"] = [alternate_title]
            normalized_record["version_role"] = "alternate_title_revision"
            normalized_record["index_html_path"] = recovered_html_rel
            normalized_record["index_pdf_path"] = recovered_entry["pdf"]["path"]
            normalized_record["decision_evidence"] = list(
                dict.fromkeys(
                    [
                        *list(normalized_record.get("decision_evidence") or []),
                        *list(recovery_evidence),
                    ]
                )
            )
        normalized.append(normalized_record)

    if domain == "video-llm-compression":
        for recovery_id in sorted(set(version_recoveries) - matched_recoveries):
            blockers.append(f"version_recovery_without_paper_record:{recovery_id}")

    canonical = _canonical_catalog(domain, normalized, baseline_papers, recovered_normalized)
    counts = {
        "baseline_canonical": len(baseline_papers),
        "source_records": len(normalized),
        "exact_reuse": exact_reuse,
        "alternate_versions": alternate_versions,
        "recovered_versions": len(recovered_normalized),
        "version_pages": alternate_versions + len(recovered_normalized),
        "canonical_papers": len(canonical),
        "generated_html": len(normalized) + len(recovered_normalized),
        "analysis_files": len(normalized),
        "evidence_files": len(normalized),
        "baseline_official_local": baseline_repository_counts["official_local"],
        "baseline_announced_pending": baseline_repository_counts["announced_pending"],
        "baseline_none_found": baseline_repository_counts["none_found"],
    }
    index_records = _index_records(canonical, normalized, baseline_repositories)
    counts["site_rows"] = len(index_records)
    blockers.extend(_validate_expected(counts, expected, domain))
    catalog = {
        "schema_version": "compression-unit.papers.v2",
        "domain": domain,
        "snapshot_date": snapshot_date,
        "build_id": build_id,
        "counts": counts,
        "expected_counts": dict(expected),
        "canonical_papers": canonical,
        "source_records": normalized,
        "recovered_versions": recovered_normalized,
        "no_guide": True,
    }
    _write_json(unit_stage / "library/catalog/papers.v2.json", catalog)
    repositories = {
        "schema_version": "compression-unit.repositories.v2",
        "domain": domain,
        "snapshot_date": snapshot_date,
        "build_id": build_id,
        "counts": baseline_repository_counts,
        "baseline_v1": baseline_repositories,
        "baseline_v1_legacy": legacy_baseline_repositories,
        "records": [
            {
                "source_record_id": record["source_record_id"],
                "canonical_paper_id": record["canonical_paper_id"],
                **record["code"],
            }
            for record in normalized
        ],
        "no_guide": True,
    }
    _write_json(unit_stage / "library/catalog/repositories.v2.json", repositories)
    _write_bytes(
        unit_stage / "library/site/assets/compression-unit-v2.css", INDEX_CSS.encode("utf-8")
    )
    _write_bytes(
        unit_stage / config["site_index"],
        _render_index(
            domain=domain,
            label=config["label"],
            snapshot_date=snapshot_date,
            counts=counts,
            records=index_records,
        ).encode("utf-8"),
    )
    _write_bytes(unit_stage / config["entry"], _render_root_entry(domain).encode("utf-8"))
    _write_bytes(
        unit_stage / config["readme"],
        _render_readme(domain, counts, snapshot_date).encode("utf-8"),
    )
    _write_bytes(unit_stage / config["validator"], VALIDATOR_SOURCE.read_bytes())

    generated_stage_paths = _invoke_html_generator(
        html_batches,
        staging_root=staging_root,
        core_root=core_root,
        domain=domain,
    )
    generated_html = tuple(
        (Path(path).relative_to(unit_stage)).as_posix() for path in generated_stage_paths
    )
    planned_without_manifest = sorted(path for path in unit_stage.rglob("*") if path.is_file())
    artifacts = [_artifact(path, unit_stage) for path in planned_without_manifest]
    if domain == "video-llm-compression":
        protected = _protected_v1_paths(unit_root)
        candidate_paths = {artifact["path"] for artifact in artifacts} | {config["manifest"]}
        collision = sorted(candidate_paths & protected)
        blockers.extend(f"vlm_v1_overwrite_planned:{path}" for path in collision)
    manifest = {
        "schema_version": "compression-unit.outputs-manifest.v2",
        "domain": domain,
        "snapshot_date": snapshot_date,
        "build_id": build_id,
        "status": "generated",
        "counts": counts,
        "expected_counts": dict(expected),
        "interfaces": {
            "entry": config["entry"],
            "site_index": config["site_index"],
            "papers": "library/catalog/papers.v2.json",
            "repositories": "library/catalog/repositories.v2.json",
            "validator": config["validator"],
        },
        "input_digests": dict(input_digests),
        "generated_html": list(generated_html),
        "artifacts": artifacts,
        "owned_paths": sorted([artifact["path"] for artifact in artifacts] + [config["manifest"]]),
        "no_guide": True,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }
    manifest["manifest_payload_sha256"] = _sha256_bytes(_json_bytes(manifest))
    _write_json(unit_stage / config["manifest"], manifest)
    planned = tuple(
        sorted(
            path.relative_to(unit_stage).as_posix()
            for path in unit_stage.rglob("*")
            if path.is_file()
        )
    )
    return UnitBuild(
        domain=domain,
        unit_root=unit_root,
        staging_root=unit_stage,
        manifest_path=unit_stage / config["manifest"],
        counts=counts,
        blockers=sorted(set(blockers)),
        warnings=sorted(set(warnings)),
        generated_html=generated_html,
        planned_files=planned,
    )


def _current_owned(unit: UnitBuild) -> tuple[set[str], dict[str, str]]:
    target_manifest = unit.unit_root / Path(DOMAIN_CONFIG[unit.domain]["manifest"])
    if not target_manifest.is_file():
        return set(), {}
    try:
        payload = json.loads(target_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(
            f"既有 builder manifest 无法读取，拒绝覆盖：{target_manifest}: {exc}"
        ) from exc
    if payload.get("schema_version") != "compression-unit.outputs-manifest.v2":
        raise BuildError(f"既有 manifest 不是本构建器拥有的 v2 文件：{target_manifest}")
    stored_payload_hash = payload.pop("manifest_payload_sha256", None)
    if stored_payload_hash != _sha256_bytes(_json_bytes(payload)):
        raise BuildError(f"既有 v2 manifest 内容已被修改或损坏，拒绝覆盖：{target_manifest}")
    owned = {str(value) for value in payload.get("owned_paths") or []}
    hashes = {
        str(item["path"]): str(item["sha256"])
        for item in payload.get("artifacts") or []
        if isinstance(item, dict) and item.get("path") and item.get("sha256")
    }
    return owned, hashes


def _preflight_assets(unit: UnitBuild, core_root: Path) -> list[str]:
    manifest = json.loads(unit.manifest_path.read_text(encoding="utf-8"))
    catalog_path = unit.staging_root / manifest["interfaces"]["papers"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    records = [*catalog["source_records"], *catalog.get("recovered_versions", [])]
    for record in records:
        source_id = record.get("version_id") or record["source_record_id"]
        pdf_path = unit.unit_root / Path(record["pdf"]["path"])
        if not _inside(pdf_path, unit.unit_root) or not pdf_path.is_file():
            errors.append(f"pdf_missing:{unit.domain}:{source_id}:{pdf_path}")
            continue
        if pdf_path.read_bytes()[:5] != b"%PDF-":
            errors.append(f"pdf_magic_invalid:{unit.domain}:{source_id}")
        if _sha256_file(pdf_path) != record["pdf"].get("sha256"):
            errors.append(f"pdf_sha256_mismatch:{unit.domain}:{source_id}")
        if record["pdf"].get("size") and pdf_path.stat().st_size != int(record["pdf"]["size"]):
            errors.append(f"pdf_size_mismatch:{unit.domain}:{source_id}")
        code = record.get("code") or {}
        if code.get("status") == "official_local":
            repo = unit.unit_root / Path(str(code.get("local_path") or ""))
            if not _inside(repo, core_root) or not repo.is_dir():
                errors.append(f"official_code_missing:{unit.domain}:{source_id}")
                continue
            completed = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            if (
                completed.returncode != 0
                or completed.stdout.strip().lower() != str(code.get("commit")).lower()
            ):
                errors.append(f"official_code_commit_mismatch:{unit.domain}:{source_id}")
    return errors


def _apply_units(units: Sequence[UnitBuild], core_root: Path) -> tuple[int, int, int]:
    blockers = [item for unit in units for item in unit.blockers]
    if blockers:
        raise BuildError("apply 被硬门禁阻断：\n" + "\n".join(blockers[:80]))
    asset_errors = [error for unit in units for error in _preflight_assets(unit, core_root)]
    if asset_errors:
        raise BuildError("正式资产预检失败：\n" + "\n".join(asset_errors[:80]))
    prepared: list[tuple[UnitBuild, Path, Path, str, bool]] = []
    created = 0
    updated = 0
    unchanged = 0
    for unit in units:
        owned, old_hashes = _current_owned(unit)
        protected = (
            _protected_v1_paths(unit.unit_root) if unit.domain == "video-llm-compression" else set()
        )
        manifest_rel = DOMAIN_CONFIG[unit.domain]["manifest"]
        for rel in unit.planned_files:
            source = unit.staging_root / Path(rel)
            target = unit.unit_root / Path(rel)
            if rel in protected:
                raise BuildError(f"拒绝覆盖 VLM v1 只读文件：{target}")
            if target.exists() and not target.is_file():
                raise BuildError(f"目标存在且不是普通文件：{target}")
            source_hash = _sha256_file(source)
            if target.is_file():
                current_hash = _sha256_file(target)
                if current_hash == source_hash:
                    prepared.append((unit, source, target, rel, False))
                    unchanged += 1
                    continue
                if rel != manifest_rel:
                    if rel not in owned or old_hashes.get(rel) != current_hash:
                        raise BuildError(f"目标不是构建器拥有的未改动 v2 文件，拒绝覆盖：{target}")
                elif rel not in owned:
                    raise BuildError(f"既有 manifest 未声明自身所有权，拒绝覆盖：{target}")
                updated += 1
            else:
                created += 1
            prepared.append((unit, source, target, rel, True))

    committed: list[tuple[Path, Path | None]] = []
    backups: list[Path] = []
    try:
        for _unit, source, target, _rel, needs_write in prepared:
            if not needs_write:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if target.exists():
                handle, backup_name = tempfile.mkstemp(
                    prefix=f".{target.name}.",
                    suffix=".compression-unit.rollback",
                    dir=target.parent,
                )
                os.close(handle)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(target, backup)
                backups.append(backup)
            try:
                os.replace(source, target)
            except Exception:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                raise
            committed.append((target, backup))

        html_paths = [unit.unit_root / Path(rel) for unit in units for rel in unit.generated_html]
        html_errors = _strict_validate_generated_html(html_paths, core_root)
        if html_errors:
            raise BuildError("apply 后严格 HTML 验收失败：\n" + "\n".join(html_errors[:80]))
        for unit in units:
            validator_path = unit.unit_root / Path(DOMAIN_CONFIG[unit.domain]["validator"])
            manifest_path = unit.unit_root / Path(DOMAIN_CONFIG[unit.domain]["manifest"])
            completed = subprocess.run(
                [
                    sys.executable,
                    str(validator_path),
                    "--unit-root",
                    str(unit.unit_root),
                    "--manifest",
                    str(manifest_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
            )
            if completed.returncode != 0:
                raise BuildError(
                    f"单元验证失败（{unit.domain}）：{completed.stderr[-2000:]}\n"
                    f"{completed.stdout[-4000:]}"
                )
    except Exception:
        for target, backup in reversed(committed):
            try:
                if backup is not None and backup.exists():
                    target.unlink(missing_ok=True)
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    else:
        for backup in backups:
            backup.unlink(missing_ok=True)
    return created, updated, unchanged


def build_units(
    *,
    paper_records: Path,
    evidence_dir: Path,
    code_audits: Sequence[Path],
    version_recovery_reports: Sequence[Path] = (),
    core_root: Path,
    staging_root: Path,
    snapshot_date: str,
    expectations_path: Path | None = None,
    apply: bool = False,
) -> BuildResult:
    if not DATE_RE.fullmatch(snapshot_date):
        raise BuildError("snapshot_date 必须是 YYYY-MM-DD。")
    core_root = core_root.resolve(strict=True)
    if not core_root.is_dir():
        raise BuildError(f"核心领域根不是目录：{core_root}")
    staging_root = _ensure_inside(staging_root, core_root, "staging 目录")
    if staging_root.exists():
        raise BuildError(f"staging 目录已存在；为避免混入旧产物，请使用新目录：{staging_root}")
    records = _load_records(paper_records)
    evidence, evidence_warnings = _load_evidence(evidence_dir)
    audits = _load_code_audit(code_audits)
    recoveries = _load_version_recoveries(version_recovery_reports)
    expectations = _load_expectations(expectations_path)
    input_digests = {
        "paper_records": _sha256_file(paper_records),
        "evidence_set": _sha256_bytes(
            _json_bytes({key: evidence[key] for key in sorted(evidence)})
        ),
        "code_audit_set": _sha256_bytes(_json_bytes({key: audits[key] for key in sorted(audits)})),
        "version_recovery_set": _sha256_bytes(
            _json_bytes({key: recoveries[key] for key in sorted(recoveries)})
        ),
        "expectations": _sha256_bytes(_json_bytes(expectations)),
    }
    build_id = _sha256_bytes(_json_bytes({"snapshot_date": snapshot_date, **input_digests}))[:20]
    staging_root.mkdir(parents=True)
    units: list[UnitBuild] = []
    for domain in DOMAIN_CONFIG:
        selected = [
            record
            for record in records
            if isinstance(record.get("domain_slugs"), list)
            and domain in record.get("domain_slugs", [])
        ]
        unit = _build_one(
            domain=domain,
            records=selected,
            evidence_by_key=evidence,
            code_audit=audits,
            version_recoveries=recoveries,
            core_root=core_root,
            staging_root=staging_root,
            snapshot_date=snapshot_date,
            build_id=build_id,
            expected=expectations[domain],
            input_digests=input_digests,
        )
        units.append(unit)
    all_blockers = sorted({item for unit in units for item in unit.blockers})
    report: dict[str, Any] = {
        "schema_version": "compression-unit-builder.report.v2",
        "status": "blocked" if all_blockers else "ready",
        "mode": "apply" if apply else "dry-run-staging",
        "build_id": build_id,
        "snapshot_date": snapshot_date,
        "staging_root": str(staging_root),
        "input_digests": input_digests,
        "evidence_scan_warnings": evidence_warnings,
        "units": [
            {
                "domain": unit.domain,
                "target_root": str(unit.unit_root),
                "staging_root": str(unit.staging_root),
                "counts": unit.counts,
                "blockers": unit.blockers,
                "warnings": unit.warnings,
                "generated_html": list(unit.generated_html),
                "planned_files": list(unit.planned_files),
            }
            for unit in units
        ],
        "blockers": all_blockers,
    }
    _write_json(staging_root / "build-report.json", report)
    created = updated = unchanged = 0
    if apply:
        created, updated, unchanged = _apply_units(units, core_root)
        report["status"] = "applied"
        report["apply_counts"] = {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
        }
        _write_json(staging_root / "build-report.json", report)
    return BuildResult(
        status=str(report["status"]),
        build_id=build_id,
        staging_root=staging_root,
        units=tuple(units),
        applied=apply,
        files_created=created,
        files_updated=updated,
        files_unchanged=unchanged,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从最终 paper-record、原文证据和代码审计构建三个压缩资料单元。"
    )
    parser.add_argument("--paper-records", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--code-audit", type=Path, action="append", required=True)
    parser.add_argument(
        "--version-recovery",
        type=Path,
        action="append",
        default=[],
        help="可重复：显式论文标题修订/双 PDF 版本恢复报告。",
    )
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--expectations", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="事务式提交 staging；默认只构建 staging，不修改三个真实单元。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_units(
            paper_records=args.paper_records,
            evidence_dir=args.evidence_dir,
            code_audits=args.code_audit,
            version_recovery_reports=args.version_recovery,
            core_root=args.core_root,
            staging_root=args.staging_root,
            snapshot_date=args.snapshot_date,
            expectations_path=args.expectations,
            apply=args.apply,
        )
        output = {
            "status": result.status,
            "build_id": result.build_id,
            "staging_root": str(result.staging_root),
            "applied": result.applied,
            "files_created": result.files_created,
            "files_updated": result.files_updated,
            "files_unchanged": result.files_unchanged,
            "units": [
                {
                    "domain": unit.domain,
                    "counts": unit.counts,
                    "blockers": unit.blockers,
                    "warnings": unit.warnings,
                }
                for unit in result.units
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if result.status in {"ready", "applied"} else 1
    except Exception as exc:
        print(f"构建失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
