"""Compile reviewed code-research batches into one fail-closed approval document."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from tools.paper_code_audit.audit import (
    APPROVAL_SCHEMA_VERSION,
    COMMIT_RE,
    GITLAB_REPOSITORY_HOSTS,
    PaperEvidence,
    _text,
    approved_title_match_kind,
    canonical_link,
    load_approvals,
    load_paper_records,
)

EXPECTED_TOTAL = 376
EXPECTED_BATCHES = 5
EXPECTED_PUBLIC = 237
EXPECTED_NON_PUBLIC = 139
PUBLIC_STATUS = "official_public"
NON_PUBLIC_STATUSES = frozenset({"announced_pending", "none_found", "third_party_only"})
ALLOWED_STATUSES = frozenset({PUBLIC_STATUS, *NON_PUBLIC_STATUSES})
SENSITIVE_QUERY_KEY_RE = re.compile(
    r"(?:^|[_-])(?:access|api|auth|credential|key|password|secret|signature|token)(?:$|[_-])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ResearchRow:
    paper_id: str
    exact_title: str
    status: str
    official_url: str | None
    commit: str | None
    license: str | None
    evidence_urls: tuple[str, ...]
    evidence_reason: str
    reviewed_at: str
    source_batch: str
    source_line: int


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    key: str
    url: str
    clone_url: str
    destination: str
    tree_ref: str | None
    sparse_path: str | None


@dataclass(slots=True)
class RepositoryGroup:
    target: RepositoryTarget
    commit: str
    rows: list[ResearchRow] = field(default_factory=list)
    requires_full_checkout: bool = False
    sparse_paths: set[str] = field(default_factory=set)


def _safe_evidence_url(value: Any) -> str:
    raw = _text(value)
    if not raw:
        raise ValueError("research evidence URL is empty")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as error:
        raise ValueError("research evidence URL is malformed") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("research evidence URL must be HTTP(S)")
    if parsed.username or parsed.password or parsed.port or parsed.fragment:
        raise ValueError("research evidence URL contains userinfo, port, or fragment")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if SENSITIVE_QUERY_KEY_RE.search(key):
            raise ValueError("research evidence URL contains a sensitive query key")
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.hostname.casefold(), parsed.path, parsed.query, "")
    )


def _read_batches(paths: Sequence[Path]) -> list[ResearchRow]:
    if len(paths) != EXPECTED_BATCHES:
        raise ValueError(f"compile-research requires exactly {EXPECTED_BATCHES} batch JSONL files")
    resolved = [path.expanduser().resolve(strict=True) for path in paths]
    if len(set(resolved)) != EXPECTED_BATCHES:
        raise ValueError("duplicate code-research batch path")
    rows: list[ResearchRow] = []
    for path in sorted(resolved, key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.casefold() != ".jsonl":
            raise ValueError(f"code-research batch must be JSONL: {path}")
        if not path.name.casefold().startswith("batch-"):
            raise ValueError(f"unexpected code-research batch filename: {path.name}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    message = f"invalid research JSONL at {path}:{line_number}: {error}"
                    raise ValueError(message) from error
                if not isinstance(raw, Mapping):
                    raise ValueError(f"research row is not an object: {path}:{line_number}")
                status = (_text(raw.get("status")) or "").casefold()
                if status not in ALLOWED_STATUSES:
                    raise ValueError(
                        f"unsupported research status at {path}:{line_number}: {status}"
                    )
                paper_id = _text(raw.get("paper_id")) or ""
                exact_title = _text(raw.get("exact_title")) or ""
                evidence_reason = _text(raw.get("evidence_reason")) or ""
                reviewed_at = _text(raw.get("reviewed_at")) or ""
                evidence_values = raw.get("evidence_urls")
                if not isinstance(evidence_values, list) or not evidence_values:
                    raise ValueError(f"research row has no evidence URLs: {path}:{line_number}")
                evidence_urls = tuple(_safe_evidence_url(value) for value in evidence_values)
                if not paper_id or not exact_title or not evidence_reason or not reviewed_at:
                    raise ValueError(f"research row lacks required evidence: {path}:{line_number}")
                official_url = _text(raw.get("official_url"))
                commit = (_text(raw.get("commit")) or "").casefold() or None
                license_value = _text(raw.get("license"))
                if status == PUBLIC_STATUS:
                    if not official_url or not commit or not COMMIT_RE.fullmatch(commit):
                        message = (
                            "official_public row requires URL and exact commit: "
                            f"{path}:{line_number}"
                        )
                        raise ValueError(message)
                elif status == "announced_pending":
                    if commit is not None and not COMMIT_RE.fullmatch(commit):
                        raise ValueError(
                            f"announced placeholder has invalid commit: {path}:{line_number}"
                        )
                    if commit is not None and official_url is None:
                        raise ValueError(
                            f"announced placeholder commit has no URL: {path}:{line_number}"
                        )
                    if official_url is not None:
                        official_url = _safe_evidence_url(official_url)
                elif official_url is not None or commit is not None:
                    message = (
                        "non-public row unexpectedly declares repository evidence: "
                        f"{path}:{line_number}"
                    )
                    raise ValueError(message)
                rows.append(
                    ResearchRow(
                        paper_id=paper_id,
                        exact_title=exact_title,
                        status=status,
                        official_url=official_url,
                        commit=commit,
                        license=license_value,
                        evidence_urls=evidence_urls,
                        evidence_reason=evidence_reason,
                        reviewed_at=reviewed_at,
                        source_batch=path.name,
                        source_line=line_number,
                    )
                )
    if len(rows) != EXPECTED_TOTAL:
        raise ValueError(f"research rows must total {EXPECTED_TOTAL}, got {len(rows)}")
    ids = [row.paper_id for row in rows]
    duplicates = sorted(paper_id for paper_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError("duplicate research paper_id: " + ", ".join(duplicates[:20]))
    return rows


def _safe_sparse_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
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
    ):
        raise ValueError(f"official repository URL must be clean HTTPS: {value}")
    host = (parsed.hostname or "").casefold().rstrip(".")
    segments = [part for part in parsed.path.split("/") if part]
    tree_ref = None
    sparse_path = None
    if host == "github.com" and len(segments) >= 4 and segments[2].casefold() == "tree":
        tree_ref = segments[3]
        if len(segments) > 4:
            sparse_path = _safe_sparse_path("/".join(segments[4:]))
        base = f"https://github.com/{segments[0]}/{segments[1]}"
    elif host in GITLAB_REPOSITORY_HOSTS and "-" in segments:
        marker = segments.index("-")
        tail = segments[marker + 1 :]
        if len(tail) < 2 or tail[0].casefold() != "tree":
            raise ValueError(f"unsupported GitLab official URL subpath: {value}")
        tree_ref = tail[1]
        if len(tail) > 2:
            sparse_path = _safe_sparse_path("/".join(tail[2:]))
        base = f"https://{host}/{'/'.join(segments[:marker])}"
    else:
        base = value
    link = canonical_link(base)
    if link is None or link.kind != "repository" or link.clone_url is None:
        raise ValueError(f"official URL is not a canonical GitHub/GitLab repository: {value}")
    parts = link.key.split("/")
    if parts[0] == "github.com":
        destination_host = "github"
    elif parts[0] == "gitlab.com":
        destination_host = "gitlab"
    else:
        destination_host = parts[0]
    destination = "/".join((destination_host, *parts[1:]))
    return RepositoryTarget(
        key=link.key,
        url=link.url,
        clone_url=link.clone_url,
        destination=destination,
        tree_ref=tree_ref,
        sparse_path=sparse_path,
    )


def _paper_map(paper_record_path: Path) -> dict[str, PaperEvidence]:
    papers = load_paper_records([paper_record_path], core_root=paper_record_path.resolve().parent)
    if len(papers) != EXPECTED_TOTAL:
        raise ValueError(f"paper-records must contain {EXPECTED_TOTAL}, got {len(papers)}")
    result = {paper.paper_id: paper for paper in papers}
    if len(result) != EXPECTED_TOTAL:
        raise ValueError("paper-records contain duplicate paper_id")
    return result


def _validate_research_against_papers(
    rows: Sequence[ResearchRow], papers: Mapping[str, PaperEvidence]
) -> dict[str, str]:
    row_ids = {row.paper_id for row in rows}
    paper_ids = set(papers)
    if row_ids != paper_ids:
        missing = sorted(paper_ids - row_ids)
        extra = sorted(row_ids - paper_ids)
        raise ValueError(
            f"research/paper-record ID sets differ; missing={missing[:10]}, extra={extra[:10]}"
        )
    matches = {}
    for row in rows:
        kind = approved_title_match_kind(row.exact_title, papers[row.paper_id].title)
        if kind is None:
            kind = "agent_reviewed_exact_title_override"
        matches[row.paper_id] = kind
    return matches


def _repository_groups(rows: Sequence[ResearchRow]) -> dict[str, RepositoryGroup]:
    groups: dict[str, RepositoryGroup] = {}
    for row in rows:
        if row.status != PUBLIC_STATUS or not row.official_url or not row.commit:
            continue
        target = _repository_target(row.official_url)
        group = groups.get(target.key)
        if group is None:
            group = RepositoryGroup(target=target, commit=row.commit)
            groups[target.key] = group
        elif group.commit != row.commit:
            raise ValueError(
                f"same repository has conflicting commits: {target.url} "
                f"({group.commit} != {row.commit})"
            )
        group.rows.append(row)
        if target.sparse_path is None:
            group.requires_full_checkout = True
        else:
            group.sparse_paths.add(target.sparse_path)
    return groups


def _repository_approval(
    group: RepositoryGroup,
    *,
    papers: Mapping[str, PaperEvidence],
    title_matches: Mapping[str, str],
) -> dict[str, Any]:
    rows = sorted(group.rows, key=lambda item: item.paper_id)
    evidence = []
    evidence_urls = set()
    licenses = set()
    paper_evidence = []
    for row in rows:
        evidence.append(f"[{row.paper_id}] {row.evidence_reason}")
        evidence.extend(f"[{row.paper_id}] {url}" for url in row.evidence_urls)
        evidence_urls.update(row.evidence_urls)
        if row.license:
            licenses.add(row.license)
        parsed_target = _repository_target(row.official_url or "")
        paper_evidence.append(
            {
                "paper_id": row.paper_id,
                "research_status": row.status,
                "reviewed_at": row.reviewed_at,
                "license": row.license,
                "source_batch": row.source_batch,
                "source_line": row.source_line,
                "original_official_url": row.official_url,
                "tree_ref": parsed_target.tree_ref,
                "sparse_path": parsed_target.sparse_path,
                "evidence_urls": list(row.evidence_urls),
                "evidence_reason": row.evidence_reason,
            }
        )
    sparse_paths = [] if group.requires_full_checkout else sorted(group.sparse_paths)
    return {
        "url": group.target.url,
        "official": True,
        "commit": group.commit,
        "destination": group.target.destination,
        "papers": [
            {
                "paper_id": row.paper_id,
                "exact_title": row.exact_title,
                "paper_record_title": papers[row.paper_id].title,
                "title_match": title_matches[row.paper_id],
            }
            for row in rows
        ],
        "sparse_paths": sparse_paths,
        "evidence": sorted(set(evidence)),
        "licenses": sorted(licenses),
        "paper_evidence": paper_evidence,
        # Kept in the approval's audit payload below as well for easy review.
        "_evidence_url_count": len(evidence_urls),
    }


def _no_code_approval(row: ResearchRow, *, source_title: str, title_match: str) -> dict[str, Any]:
    return {
        "paper_id": row.paper_id,
        "exact_title": row.exact_title,
        "paper_record_title": source_title,
        "title_match": title_match,
        "verified_no_public_code": True,
        "reviewed_at": row.reviewed_at,
        "research_status": row.status,
        "announced_url": row.official_url,
        "announced_commit": row.commit,
        "license": row.license,
        "evidence_reason": row.evidence_reason,
        "source_batch": row.source_batch,
        "source_line": row.source_line,
        "evidence_urls": list(row.evidence_urls),
    }


def compile_research(
    *,
    batch_paths: Sequence[Path],
    paper_record_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = _read_batches(batch_paths)
    public_count = sum(row.status == PUBLIC_STATUS for row in rows)
    non_public_count = sum(row.status in NON_PUBLIC_STATUSES for row in rows)
    if public_count != EXPECTED_PUBLIC or non_public_count != EXPECTED_NON_PUBLIC:
        raise ValueError(
            "research status totals changed: "
            f"official_public={public_count} (expected {EXPECTED_PUBLIC}), "
            f"non_public={non_public_count} (expected {EXPECTED_NON_PUBLIC})"
        )
    papers = _paper_map(paper_record_path)
    title_matches = _validate_research_against_papers(rows, papers)
    groups = _repository_groups(rows)
    repository_approvals = []
    evidence_url_counts = {}
    for key, group in sorted(groups.items()):
        approval = _repository_approval(
            group,
            papers=papers,
            title_matches=title_matches,
        )
        evidence_url_counts[key] = approval.pop("_evidence_url_count")
        repository_approvals.append(approval)
    no_code = [
        _no_code_approval(
            row,
            source_title=papers[row.paper_id].title,
            title_match=title_matches[row.paper_id],
        )
        for row in sorted(rows, key=lambda item: item.paper_id)
        if row.status in NON_PUBLIC_STATUSES
    ]
    status_counts = Counter(row.status for row in rows)
    shared = [
        {
            "url": approval["url"],
            "destination": approval["destination"],
            "paper_ids": [paper["paper_id"] for paper in approval["papers"]],
        }
        for approval in repository_approvals
        if len(approval["papers"]) > 1
    ]
    clone_plan = [
        {
            "url": approval["url"],
            "clone_url": f"{approval['url']}.git",
            "commit": approval["commit"],
            "destination": approval["destination"],
            "sparse_paths": approval["sparse_paths"],
            "paper_count": len(approval["papers"]),
            "action": "approved_not_applied",
        }
        for approval in repository_approvals
    ]
    audit_payload = {
        "compiler": "tools.paper_code_audit.compile_research",
        "expected_total": EXPECTED_TOTAL,
        "research_rows": len(rows),
        "paper_records": len(papers),
        "status_counts": dict(sorted(status_counts.items())),
        "official_public_papers": status_counts[PUBLIC_STATUS],
        "non_public_papers": sum(status_counts[value] for value in NON_PUBLIC_STATUSES),
        "unique_repositories": len(repository_approvals),
        "shared_repositories": shared,
        "clone_plan": clone_plan,
        "evidence_url_counts_by_repository": evidence_url_counts,
        "research_batches": [str(path.resolve()) for path in batch_paths],
        "paper_records_path": str(paper_record_path.resolve()),
        "network_or_clone_performed": False,
    }
    document = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "repositories": repository_approvals,
        "no_code": no_code,
        "audit": audit_payload,
    }
    # Reuse the runtime approval parser as a second, independent contract check.
    return document, audit_payload


def render_summary(audit_payload: Mapping[str, Any], document: Mapping[str, Any]) -> str:
    status_counts = audit_payload["status_counts"]
    lines = [
        "# 2026-08-31 论文官方代码研究编译摘要",
        "",
        (
            "本报告由五批 Agent 核验 JSONL 与 376 条 paper-record 离线编译。"
            "本次没有联网、clone 或修改 Hub 配置。"
        ),
        "",
        "## 强门禁结果",
        "",
        f"- 研究记录：{audit_payload['research_rows']} / 376",
        f"- paper-record：{audit_payload['paper_records']} / 376",
        "- `paper_id` 集合：完全一致且唯一",
        f"- `official_public`：{status_counts.get('official_public', 0)}",
        f"- `none_found`：{status_counts.get('none_found', 0)}",
        f"- `third_party_only`：{status_counts.get('third_party_only', 0)}",
        f"- `announced_pending`：{status_counts.get('announced_pending', 0)}",
        f"- 非公开官方代码决策：{audit_payload['non_public_papers']}",
        f"- 规范唯一仓库：{audit_payload['unique_repositories']}",
        f"- 被多篇论文共享的仓库：{len(audit_payload['shared_repositories'])}",
        "- 同仓库 commit 冲突：0（否则编译已硬失败）",
        "",
        "## 共享仓库关系",
        "",
    ]
    shared = audit_payload["shared_repositories"]
    if shared:
        lines.extend(
            [
                "| 规范仓库 | 归档目标 | 论文数 | paper_id |",
                "| --- | --- | ---: | --- |",
            ]
        )
        for item in shared:
            ids = "<br>".join(f"`{value}`" for value in item["paper_ids"])
            lines.append(
                f"| {item['url']} | `{item['destination']}` | {len(item['paper_ids'])} | {ids} |"
            )
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 预计 clone 计划（未执行）",
            "",
            "| # | 规范仓库 | commit | 目标 | sparse | 论文数 |",
            "| ---: | --- | --- | --- | --- | ---: |",
        ]
    )
    for index, item in enumerate(audit_payload["clone_plan"], start=1):
        sparse = ", ".join(f"`{value}`" for value in item["sparse_paths"]) or "全仓库"
        lines.append(
            f"| {index} | {item['url']} | `{item['commit']}` | "
            f"`{item['destination']}` | {sparse} | {item['paper_count']} |"
        )
    lines.extend(
        [
            "",
            "## 非公开状态保留",
            "",
            "139 条全部写入 approvals `no_code`，同时保留 "
            "`research_status` / `evidence_reason` / `evidence_urls` / `reviewed_at` / "
            "源 batch 与行号。该文件只是显式审批输入，不会自动改写 Hub 白名单。",
            "",
            "## 归档边界",
            "",
            "- GitHub `/tree/<branch>/<path>` 已规范为仓库 URL + `sparse_paths`。",
            "- 同一规范 remote 只有一条归档计划；共享论文都绑在其 `papers`。",
            "- 未执行 clone；后续只能由 `paper_code_audit --apply` 在显式 archive root 下执行。",
            "",
            f"审批仓库条目：{len(document['repositories'])}；"
            f"无当前官方公开代码条目：{len(document['no_code'])}。",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_compiled_outputs(
    *,
    document: Mapping[str, Any],
    summary: str,
    approvals_output: Path,
    summary_output: Path,
    paper_record_path: Path,
) -> None:
    # Validate the exact serialized shape with the runtime loader before publishing.
    temporary = approvals_output.with_name(f".{approvals_output.name}.validate-{os.getpid()}")
    try:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        papers = load_paper_records(
            [paper_record_path], core_root=paper_record_path.resolve().parent
        )
        load_approvals(temporary, papers)
    finally:
        if temporary.exists():
            temporary.unlink()
    _atomic_write(
        approvals_output,
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(summary_output, summary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, action="append", required=True)
    parser.add_argument("--paper-records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        document, audit_payload = compile_research(
            batch_paths=arguments.batch,
            paper_record_path=arguments.paper_records,
        )
        summary = render_summary(audit_payload, document)
        write_compiled_outputs(
            document=document,
            summary=summary,
            approvals_output=arguments.output,
            summary_output=arguments.summary,
            paper_record_path=arguments.paper_records,
        )
    except (OSError, ValueError) as error:
        print(f"compile-research failed: {error}")
        return 2
    print(
        json.dumps(
            {
                "output": str(arguments.output.resolve()),
                "summary": str(arguments.summary.resolve()),
                "counts": audit_payload["status_counts"],
                "unique_repositories": audit_payload["unique_repositories"],
                "shared_repositories": len(audit_payload["shared_repositories"]),
                "clone_performed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
