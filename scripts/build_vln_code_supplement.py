"""Build the reviewed code-only VLN supplement without changing earlier sources."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apps.api.config import PROJECT_ROOT
from packages.domain.exporters.common import atomic_write_json
from packages.ingestion.safe_tree_policy import content_tree_digest, scan_directory

SOURCE_ID = "vln-release-status-code-supplement-20260912"
AUDIT_ROOT = PROJECT_ROOT / "work" / "validation" / "vln-code-research-20260912"
CODE_ROOT = PROJECT_ROOT.parent / "共享代码" / "2026-09-12-vln-code-supplement" / "upstream"
REPOSITORIES = {
    "01a06831-3d19-7a11-9f8a-e817e81f49aa": (
        "LookStep", "https://github.com/kunyang-YU/LookStep",
    ),
    "01a0685f-869b-7a34-a8de-8a9eed75da50": (
        "FlowDec",
        "https://github.com/FeeFee-1/FlowDec-Temporal-Conditional-Flow-Decorruptor-for-Robust-Continuous-Vision-Language-Navigation",
    ),
}
CODE_STATES = {
    "public_implementation": "official_open",
    "official_placeholder": "official_placeholder",
    "announced_unreleased": "official_announced",
    "unknown": "unknown",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *arguments],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def _github_repository(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.netloc != "github.com" or len(parts) != 2:
        return None
    return f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}"


def main() -> int:
    inventory = _json(PROJECT_ROOT / "work" / "mineru" / "vln-20260904" / "inventory.json")
    papers = {row["asset_id"]: row for row in inventory["items"]}
    prior = _json(
        PROJECT_ROOT / "work" / "validation" / "vln-code-action-existing-evidence-20260912.json"
    )
    expected = {row["asset_id"] for row in prior["items"]}
    investigations = [
        item
        for group in ("01", "02", "03")
        for item in _json(AUDIT_ROOT / f"group-{group}.json")["items"]
    ]
    if len(investigations) != 27 or {row["asset_id"] for row in investigations} != expected:
        raise ValueError("Code investigation must cover exactly the 27 reviewed paper IDs")
    reviews = {}
    for finding in _json(AUDIT_ROOT / "review.json")["findings"]:
        for asset_id in finding.get("asset_ids", [finding.get("asset_id")]):
            reviews[asset_id] = finding
    if not expected <= reviews.keys():
        raise ValueError("Independent review coverage is incomplete")
    public = {
        row["asset_id"] for row in investigations
        if reviews[row["asset_id"]]["recommended_status"] == "public_implementation"
    }
    if public != REPOSITORIES.keys():
        raise ValueError("Public implementation decisions differ from the reviewed clone set")

    items = []
    materialized = []
    statuses: Counter[str] = Counter()
    for investigation in investigations:
        asset_id = investigation["asset_id"]
        paper = papers[asset_id]
        review = reviews[asset_id]
        status = review["recommended_status"]
        statuses[status] += 1
        if investigation["title"] != paper["title"]:
            raise ValueError(f"Paper title mismatch: {asset_id}")
        evidence = [*investigation["evidence"], *review["evidence"]]
        evidence_urls = list(dict.fromkeys(item["url"] for item in evidence))
        repository_url = _github_repository(investigation.get("repo_url"))
        representations = {}
        if status == "public_implementation":
            relative_path, expected_url = REPOSITORIES[asset_id]
            path = CODE_ROOT / relative_path
            if repository_url != expected_url:
                raise ValueError(f"Reviewed repository URL mismatch: {asset_id}")
            if _github_repository(_git(path, "remote", "get-url", "origin")) != expected_url:
                raise ValueError(f"Clone origin mismatch: {asset_id}")
            if _git(path, "status", "--porcelain"):
                raise ValueError(f"Clone contains modified or untracked files: {asset_id}")
            commit = _git(path, "rev-parse", "HEAD")
            skipped: list[dict[str, str]] = []
            errors: list[dict[str, str]] = []
            files = scan_directory(path, path, skipped, errors)
            if errors:
                raise ValueError(f"Repository safe-tree scan failed: {asset_id}: {errors}")
            tree = {
                "tree_sha256": content_tree_digest(files),
                "file_count": len(files), "skipped": skipped,
            }
            representations["code"] = {
                "path": relative_path,
                "url": expected_url,
                "commit": commit,
                "license": "NOASSERTION",
                "status": "official_local",
            }
            materialized.append({
                "asset_id": asset_id, "repository_url": expected_url,
                "path": relative_path, "commit": commit, "safe_tree": tree,
                "code_executed": False, "dependencies_installed": False,
                "external_checkpoints_downloaded": False,
            })
        identifiers = {
            item["scheme"]: item["value"] for item in paper["identifiers"]
            if item["is_identity"] and item["scheme"] in {"doi", "arxiv"}
        }
        note = (
            investigation["implementation_scope"] + " " + review["correction"]
            + " 本次仅核查代码；Checkpoint 状态沿用既有记录，未重新核验。"
        )
        items.append({
            "id": f"code-audit-{asset_id}",
            **{key: paper[key] for key in ("title", "title_zh", "authors", "year", "venue")},
            "doi": identifiers.get("doi"), "arxiv_id": identifiers.get("arxiv"),
            "source_urls": list(dict.fromkeys([
                *paper["source_urls"], *([repository_url] if repository_url else []),
            ])),
            "tags": ["vision-language-navigation", "code-release-audit"],
            "attributes": {
                "release_status": {
                    "code_open": CODE_STATES[status],
                    "code_local": "full" if representations else "absent",
                    "code_verified_at": "2026-09-12",
                    "repository_url": repository_url,
                    "code_evidence_urls": evidence_urls,
                    "note": note,
                },
                "code_audit": {
                    "source_asset_id": asset_id, "checked_at": "2026-09-12",
                    "status": status, "independently_reviewed": True,
                    "implementation_scope": investigation["implementation_scope"],
                    "review_verdict": review["verdict"],
                    "review_note": review["correction"], "evidence": evidence,
                    "asserts_global_no_public_code": False,
                },
            },
            "representations": representations,
        })
    document = {
        "schema_version": "research-asset-hub.generic-manifest/v1",
        "source_id": SOURCE_ID, "audited_at": "2026-09-12", "items": items,
    }
    destination = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"
    if destination.exists() and _json(destination) != document:
        raise ValueError("The supplement manifest already exists with different content")
    if not destination.exists():
        atomic_write_json(destination, document)
    report = {
        "status": "passed", "source_id": SOURCE_ID, "paper_audits": len(items),
        "new_code_snapshots": len(materialized), "statuses": dict(statuses),
        "existing_sources_modified": False, "code_executed": False,
        "materialized": materialized,
    }
    atomic_write_json(AUDIT_ROOT / "materialization.json", report)
    print(json.dumps({key: value for key, value in report.items() if key != "materialized"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
