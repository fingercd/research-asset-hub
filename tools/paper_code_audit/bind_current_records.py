"""Bind reviewed code decisions to the exact titles in current paper records."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.paper_code_audit.audit import (
    APPROVAL_SCHEMA_VERSION,
    PaperEvidence,
    _text,
    load_approvals,
    load_paper_records,
)

EXPECTED_TOTAL = 376


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_approval_document(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"research approvals path is not a file: {resolved}")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid research approvals JSON: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("research approvals must be one JSON object")
    if document.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        raise ValueError(f"research approvals schema must be {APPROVAL_SCHEMA_VERSION}")
    if not isinstance(document.get("repositories"), list) or not isinstance(
        document.get("no_code"), list
    ):
        raise ValueError("research approvals require repositories and no_code arrays")
    return document


def _current_papers(path: Path) -> dict[str, PaperEvidence]:
    papers = load_paper_records([path], core_root=path.resolve().parent)
    if len(papers) != EXPECTED_TOTAL:
        raise ValueError(f"current paper-records must contain {EXPECTED_TOTAL}, got {len(papers)}")
    result = {paper.paper_id: paper for paper in papers}
    if len(result) != EXPECTED_TOTAL:
        raise ValueError("current paper-records contain duplicate paper_id")
    return result


def _repository_bindings(
    document: Mapping[str, Any],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[str]]:
    pairs = []
    ids = []
    for repository_index, raw_repository in enumerate(document["repositories"], start=1):
        if not isinstance(raw_repository, dict):
            raise ValueError(f"repository approval {repository_index} is not an object")
        papers = raw_repository.get("papers")
        evidence = raw_repository.get("paper_evidence")
        if not isinstance(papers, list) or not papers:
            raise ValueError(f"repository approval {repository_index} has no paper bindings")
        if not isinstance(evidence, list):
            raise ValueError(f"repository approval {repository_index} has no per-paper evidence")
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError(
                    f"repository approval {repository_index} has invalid per-paper evidence"
                )
            paper_id = _text(item.get("paper_id")) or ""
            if not paper_id or paper_id in evidence_by_id:
                raise ValueError(
                    f"repository approval {repository_index} has duplicate/missing evidence ID"
                )
            if (
                not item.get("evidence_urls")
                or not _text(item.get("evidence_reason"))
                or not _text(item.get("reviewed_at"))
            ):
                raise ValueError(f"repository paper evidence is incomplete: {paper_id}")
            evidence_by_id[paper_id] = item
        binding_ids = set()
        for binding in papers:
            if not isinstance(binding, dict):
                raise ValueError(f"repository approval {repository_index} has invalid binding")
            paper_id = _text(binding.get("paper_id")) or ""
            if not paper_id or paper_id in binding_ids:
                raise ValueError(
                    f"repository approval {repository_index} has duplicate/missing paper ID"
                )
            if not _text(binding.get("exact_title")) or not _text(binding.get("title_match")):
                raise ValueError(f"repository binding lacks reviewed title evidence: {paper_id}")
            evidence_item = evidence_by_id.get(paper_id)
            if evidence_item is None:
                raise ValueError(f"repository binding has no same-ID evidence: {paper_id}")
            binding_ids.add(paper_id)
            ids.append(paper_id)
            pairs.append((binding, evidence_item))
        extra_evidence = sorted(set(evidence_by_id) - binding_ids)
        if extra_evidence:
            raise ValueError(
                "repository evidence has no same-ID paper binding: "
                + ", ".join(extra_evidence[:20])
            )
    return pairs, ids


def _no_code_bindings(document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    result = []
    ids = []
    for index, item in enumerate(document["no_code"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"no_code approval {index} is not an object")
        paper_id = _text(item.get("paper_id")) or ""
        if (
            not paper_id
            or not _text(item.get("exact_title"))
            or not _text(item.get("title_match"))
            or not item.get("evidence_urls")
            or not _text(item.get("evidence_reason"))
            or not _text(item.get("reviewed_at"))
        ):
            raise ValueError(f"no_code binding/evidence is incomplete: {paper_id or index}")
        result.append(item)
        ids.append(paper_id)
    return result, ids


def _rebind_title(binding: dict[str, Any], paper: PaperEvidence) -> None:
    research_exact_title = _text(binding.get("research_exact_title")) or _text(
        binding.get("exact_title")
    )
    if not research_exact_title:
        raise ValueError(f"binding has no research exact title: {paper.paper_id}")
    previous_record_title = _text(binding.get("paper_record_title"))
    binding["research_exact_title"] = research_exact_title
    if previous_record_title:
        binding["research_paper_record_title"] = previous_record_title
    binding["exact_title"] = paper.title
    binding["paper_record_title"] = paper.title
    binding["runtime_title_binding"] = "exact_current_record"


def bind_current_records(
    *,
    research_approvals_path: Path,
    paper_record_path: Path,
) -> dict[str, Any]:
    source = _load_approval_document(research_approvals_path)
    current = _current_papers(paper_record_path)
    # The research approvals must already be semantically valid before rebinding.
    load_approvals(research_approvals_path, list(current.values()))
    document = copy.deepcopy(source)
    repository_pairs, repository_ids = _repository_bindings(document)
    no_code_items, no_code_ids = _no_code_bindings(document)
    all_ids = [*repository_ids, *no_code_ids]
    duplicates = sorted(paper_id for paper_id, count in Counter(all_ids).items() if count > 1)
    if duplicates:
        raise ValueError("approval paper_id appears more than once: " + ", ".join(duplicates[:20]))
    approval_ids = set(all_ids)
    current_ids = set(current)
    if approval_ids != current_ids:
        missing = sorted(current_ids - approval_ids)
        extra = sorted(approval_ids - current_ids)
        raise ValueError(
            f"approval/current paper_id sets differ; missing={missing[:10]}, extra={extra[:10]}"
        )
    if len(all_ids) != EXPECTED_TOTAL:
        raise ValueError(f"runtime approvals must bind {EXPECTED_TOTAL}, got {len(all_ids)}")
    for binding, evidence in repository_pairs:
        paper_id = str(binding["paper_id"])
        if str(evidence["paper_id"]) != paper_id:
            raise ValueError(f"evidence ID changed during binding: {paper_id}")
        _rebind_title(binding, current[paper_id])
    for binding in no_code_items:
        _rebind_title(binding, current[str(binding["paper_id"])])
    audit = document.get("audit")
    if not isinstance(audit, dict):
        audit = {}
        document["audit"] = audit
    audit["runtime_binding"] = {
        "binder": "tools.paper_code_audit.bind_current_records",
        "research_approvals_path": str(research_approvals_path.resolve()),
        "research_approvals_sha256": _sha256(research_approvals_path),
        "paper_records_path": str(paper_record_path.resolve()),
        "paper_records_sha256": _sha256(paper_record_path),
        "paper_ids": EXPECTED_TOTAL,
        "repository_bindings": len(repository_ids),
        "no_code_bindings": len(no_code_ids),
        "evidence_id_rebindings": 0,
        "network_or_clone_performed": False,
    }
    return document


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_runtime_approvals(
    *,
    document: Mapping[str, Any],
    output: Path,
    paper_record_path: Path,
) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    validation_path = output.with_name(f".{output.name}.validate-{os.getpid()}")
    try:
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        validation_path.write_text(payload, encoding="utf-8")
        papers = load_paper_records(
            [paper_record_path], core_root=paper_record_path.resolve().parent
        )
        load_approvals(validation_path, papers)
    finally:
        if validation_path.exists():
            validation_path.unlink()
    _atomic_write(output, payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-approvals", type=Path, required=True)
    parser.add_argument("--paper-records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        document = bind_current_records(
            research_approvals_path=arguments.research_approvals,
            paper_record_path=arguments.paper_records,
        )
        write_runtime_approvals(
            document=document,
            output=arguments.output,
            paper_record_path=arguments.paper_records,
        )
    except (OSError, ValueError) as error:
        print(f"bind-current-records failed: {error}")
        return 2
    binding = document["audit"]["runtime_binding"]
    print(
        json.dumps(
            {
                "output": str(arguments.output.resolve()),
                "paper_ids": binding["paper_ids"],
                "repository_bindings": binding["repository_bindings"],
                "no_code_bindings": binding["no_code_bindings"],
                "network_or_clone_performed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
