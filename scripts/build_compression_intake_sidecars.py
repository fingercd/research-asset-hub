"""Build immutable generic-manifest sidecars for the three compression domains.

The builder consumes the reviewed paper-record JSONL and the frozen 2026-07-28
VideoLLM catalog. It never invents representation hashes: every local PDF/HTML
path must exist and is hashed from bytes. Official repository URLs are retained;
a local repository path is emitted only when that directory actually exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from packages.adapters.base import stable_uuid7
from packages.domain.exporters.common import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT.parent.resolve()
DEFAULT_RECORDS = (
    PROJECT_ROOT / "work" / "validation" / "2026-08-31-paper-records-final.jsonl"
)
INTAKE_ROOT = PROJECT_ROOT / "config" / "intake"
SHARED_REPOSITORY_ROOT = (
    CORE_ROOT / "共享代码" / "2026-08-31-顶会压缩论文" / "upstream"
)

SOURCE_SPECS = {
    "video-llm-compression-20260831": (
        "video-llm-compression",
        CORE_ROOT / "VLM框架3DV压缩",
        95,
    ),
    "embodied-ai-compression-20260831": (
        "embodied-ai-compression",
        CORE_ROOT / "具身智能压缩",
        44,
    ),
    "navigation-compression-20260831": (
        "navigation-compression",
        CORE_ROOT / "导航压缩",
        20,
    ),
}
BASELINE_SOURCE_ID = "video-llm-compression-v1-20260728"
BASELINE_ROOT = CORE_ROOT / "VLM框架3DV压缩"
ENRICHMENT_SOURCE_ID = "video-llm-compression-enrichment-20260831"
ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})", re.I)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PORTAL_CONFIG_ROOT = PROJECT_ROOT / "config" / "domain-portals" / "v2"
PORTAL_INPUTS = {
    "video-llm-compression": (
        "视频大模型压缩",
        "面向 VideoLLM 的帧选择、视觉 token 压缩、KV/记忆管理与流式推理。",
        (
            BASELINE_SOURCE_ID,
            "video-llm-compression-20260831",
            ENRICHMENT_SOURCE_ID,
        ),
    ),
    "embodied-ai-compression": (
        "具身智能压缩",
        "面向具身智能体与 VLA 的视觉、记忆、规划和行动表示压缩。",
        ("embodied-ai-compression-20260831",),
    ),
    "navigation-compression": (
        "导航压缩",
        "面向 VLN 与具身导航的地图、观测、历史记忆与路径计算压缩。",
        ("navigation-compression-20260831",),
    ),
}
PORTAL_SECTIONS = (
    "scope",
    "timeline",
    "capability-method-map",
    "landmarks",
    "benchmarks",
    "reading-routes",
    "catalog",
    "frontiers",
    "evidence",
)
PAPER_SOURCE_IDS = {
    BASELINE_SOURCE_ID,
    ENRICHMENT_SOURCE_ID,
    *SOURCE_SPECS.keys(),
}
PORTAL_YAML_FILES = (
    "portal.yaml",
    "timeline.yaml",
    "capability-method-map.yaml",
    "landmarks.yaml",
    "benchmarks.yaml",
    "reading-routes.yaml",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path, root: Path, *, label: str) -> str:
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(root.resolve(strict=True)).as_posix()
    except ValueError as error:
        raise ValueError(f"{label} path escapes its declared root: {resolved}") from error


def _source_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if not isinstance(value, list):
        return urls
    for item in value:
        candidate = item.get("url") if isinstance(item, dict) else item
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            if candidate not in urls:
                urls.append(candidate)
    return urls


def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"record {line_number} is not an object")
            records.append(value)
    return records


def _new_item(
    record: dict[str, Any],
    unit_root: Path,
    unit_source_record: dict[str, Any],
) -> dict[str, Any]:
    paper_id = str(record["paper_id"])
    paper_root = unit_root / "library" / "papers"
    html_root = unit_root / "library" / "site"
    unit_pdf = unit_source_record.get("pdf")
    unit_pdf = unit_pdf if isinstance(unit_pdf, dict) else {}
    pdf_path = unit_root / Path(str(unit_pdf.get("path") or ""))
    html_path = unit_root / Path(str(unit_source_record.get("html_path") or ""))
    pdf_ref = _relative(pdf_path, paper_root, label=f"{paper_id} PDF")
    html_ref = _relative(html_path, html_root, label=f"{paper_id} HTML")
    expected_pdf_sha = str(unit_pdf.get("sha256") or record["pdf"]["sha256"])
    actual_pdf_sha = _sha256(pdf_path)
    if actual_pdf_sha != expected_pdf_sha:
        raise ValueError(
            f"{paper_id} PDF digest mismatch: {actual_pdf_sha} != {expected_pdf_sha}"
        )

    source_urls = _source_urls(record.get("source_urls"))
    raw_code = record.get("code")
    code: dict[str, Any] = raw_code if isinstance(raw_code, dict) else {}
    code_url = code.get("url")
    if isinstance(code_url, str) and code_url.startswith(("http://", "https://")):
        if code_url not in source_urls:
            source_urls.append(code_url)
    else:
        code_url = None

    representations: dict[str, dict[str, Any]] = {
        "pdf": {
            "path": pdf_ref,
            "sha256": actual_pdf_sha,
            "size_bytes": pdf_path.stat().st_size,
            "page_count": unit_pdf.get("page_count") or record["pdf"].get("page_count"),
        },
        "html": {
            "path": html_ref,
            "sha256": _sha256(html_path),
            "size_bytes": html_path.stat().st_size,
        },
    }
    if code_url:
        code_representation: dict[str, Any] = {
            "url": code_url,
            "status": code.get("status"),
            "commit": code.get("commit"),
            "license": code.get("license"),
            "evidence": code.get("evidence") or [],
        }
        local_value = code.get("local_path")
        if isinstance(local_value, str) and local_value.strip():
            local_repository = Path(local_value)
            if not local_repository.is_absolute():
                local_repository = unit_root / local_repository
            if local_repository.is_dir():
                try:
                    code_representation["path"] = local_repository.resolve().relative_to(
                        SHARED_REPOSITORY_ROOT.resolve(strict=True)
                    ).as_posix()
                except ValueError:
                    # A clean repository already represented by the frozen v1
                    # source is reused by URL/identity; this new source must not
                    # invent an escaping path under the shared root.
                    pass
        representations["code"] = code_representation

    return {
        "id": str(record.get("source_record_id") or paper_id),
        "title": record["title"],
        "title_zh": record.get("title_zh"),
        "authors": record.get("authors") or [],
        "year": record.get("year"),
        "venue": record.get("venue"),
        "doi": record.get("doi"),
        "arxiv_id": record.get("arxiv_id"),
        "abstract": record.get("abstract"),
        "summary": record.get("summary"),
        "tags": record.get("tags") or [],
        "source_urls": source_urls,
        "source_url": source_urls[0] if source_urls else None,
        "representations": representations,
        "provenance": {
            "record_schema": record.get("schema_version"),
            "source_group": record.get("source_group"),
            "disposition": record.get("disposition"),
            "code_research_status": code.get("research_status"),
        },
    }


def _extract_arxiv(*values: Any) -> str | None:
    for value in values:
        if not isinstance(value, str):
            continue
        match = ARXIV_RE.search(value)
        if match:
            return match.group(1)
    return None


def _unit_source_records(unit_root: Path) -> dict[str, dict[str, Any]]:
    path = unit_root / "library" / "catalog" / "papers.v2.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    values = document.get("source_records") if isinstance(document, dict) else None
    if not isinstance(values, list):
        raise ValueError(f"unit v2 catalog has no source_records: {path}")
    result = {
        str(item.get("source_record_id")): dict(item)
        for item in values
        if isinstance(item, dict) and item.get("source_record_id")
    }
    if len(result) != len(values):
        raise ValueError(f"unit v2 catalog has duplicate/missing source_record_id: {path}")
    return result


def _baseline_items() -> list[dict[str, Any]]:
    catalog_path = BASELINE_ROOT / "library" / "catalog" / "papers.v1.json"
    document = json.loads(catalog_path.read_text(encoding="utf-8"))
    papers = document.get("papers")
    if not isinstance(papers, list) or len(papers) != 33:
        raise ValueError("frozen VideoLLM baseline must contain exactly 33 papers")
    items: list[dict[str, Any]] = []
    for raw in papers:
        paper_id = str(raw["id"])
        pdf_status = raw.get("pdf_status") or {}
        repository_status = raw.get("repository_status") or {}
        pdf_path = BASELINE_ROOT / str(raw["pdf_local"])
        html_path = BASELINE_ROOT / str(raw["analysis_html"])
        pdf_root = BASELINE_ROOT / "library" / "papers"
        html_root = BASELINE_ROOT / "library" / "site"
        repository_root = BASELINE_ROOT / "library" / "code" / "upstream"
        pdf_digest = _sha256(pdf_path)
        expected_digest = pdf_status.get("sha256")
        if expected_digest and pdf_digest != expected_digest:
            raise ValueError(f"{paper_id} frozen baseline PDF digest mismatch")
        representations: dict[str, dict[str, Any]] = {
            "pdf": {
                "path": _relative(pdf_path, pdf_root, label=f"{paper_id} PDF"),
                "url": raw.get("pdf"),
                "sha256": pdf_digest,
                "size_bytes": pdf_path.stat().st_size,
                "page_count": pdf_status.get("pages"),
            },
            "html": {
                "path": _relative(html_path, html_root, label=f"{paper_id} HTML"),
                "sha256": _sha256(html_path),
                "size_bytes": html_path.stat().st_size,
            },
        }
        repo_url = repository_status.get("url") or raw.get("repo")
        if isinstance(repo_url, str) and repo_url.startswith(("http://", "https://")):
            code: dict[str, Any] = {
                "url": repo_url,
                "status": repository_status.get("status"),
                "commit": repository_status.get("commit"),
            }
            repository_path = repository_root / paper_id
            if repository_path.is_dir():
                code["path"] = paper_id
            representations["code"] = code
        source_urls = [
            value
            for value in (raw.get("page"), raw.get("pdf"), repo_url)
            if isinstance(value, str) and value.startswith(("http://", "https://"))
        ]
        items.append(
            {
                "id": paper_id,
                "title": raw["title"],
                "title_zh": None,
                "authors": [raw["authors"]] if raw.get("authors") else [],
                "year": raw.get("year"),
                "venue": raw.get("venue"),
                "doi": None,
                "arxiv_id": _extract_arxiv(raw.get("page"), raw.get("pdf")),
                "abstract": None,
                "summary": raw.get("summary"),
                "tags": [
                    "video-llm-compression",
                    "baseline-2026-07-28",
                    str(raw.get("scope") or "unclassified"),
                ],
                "source_urls": list(dict.fromkeys(source_urls)),
                "source_url": raw.get("page"),
                "representations": representations,
                "provenance": {
                    "catalog": "library/catalog/papers.v1.json",
                    "scope": raw.get("scope"),
                    "status": raw.get("status"),
                },
            }
        )
    return items


def _enrichment_item(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    record = next(item for item in records if item.get("paper_id") == "emcompress-2026")
    versions = record.get("pdf_representations")
    if not isinstance(versions, list):
        raise ValueError("EMCompress record has no reviewed PDF-version evidence")
    alternate = next(
        (
            item
            for item in versions
            if item.get("role") == "alternate_legacy_tvs"
        ),
        None,
    )
    if not isinstance(alternate, dict):
        raise ValueError("EMCompress alternate retrieved version is missing")
    paper_root = BASELINE_ROOT / "library" / "papers"
    html_root = BASELINE_ROOT / "library" / "site"
    pdf_path = Path(str(alternate["path"]))
    html_path = (
        html_root / "versions" / "emcompress-2026" / "legacy" / "emcompress-2026.html"
    )
    digest = _sha256(pdf_path)
    if digest != alternate.get("sha256"):
        raise ValueError("EMCompress alternate PDF digest does not match reviewed evidence")
    source_url = alternate.get("source_url")
    return {
        "id": "emcompress-2026-arxiv-2508-21094",
        "title": alternate.get("title") or "Video-LLMs with Temporal Visual Screening",
        "title_zh": None,
        "authors": record.get("authors") or [],
        "year": record.get("year"),
        "venue": record.get("venue"),
        "doi": record.get("doi"),
        "arxiv_id": record.get("arxiv_id"),
        "abstract": record.get("abstract"),
        "summary": record.get("summary"),
        "tags": [
            "video-llm-compression",
            "alternate-version",
            "identity-merge-review-required",
        ],
        "source_urls": [source_url] if isinstance(source_url, str) else [],
        "source_url": source_url,
        "representations": {
            "pdf": {
                "path": _relative(pdf_path, paper_root, label="EMCompress TVS PDF"),
                "url": source_url,
                "sha256": digest,
                "size_bytes": pdf_path.stat().st_size,
                "page_count": alternate.get("page_count"),
            },
            "html": {
                "path": _relative(html_path, html_root, label="EMCompress TVS HTML"),
                "sha256": _sha256(html_path),
                "size_bytes": html_path.stat().st_size,
            },
        },
        "provenance": {
            "canonical_record_id": "emcompress-2026",
            "version_relation": record.get("version_relation"),
            "identity_evidence": record.get("decision_evidence") or [],
        },
    }


def _write(source_id: str, items: list[dict[str, Any]]) -> Path:
    output = INTAKE_ROOT / source_id / "manifest.json"
    atomic_write_json(output, {"items": items})
    return output


def _collect_asset_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if UUID_RE.fullmatch(value) else set()
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_collect_asset_ids(child))
        return result
    if isinstance(value, dict):
        result = set()
        for child in value.values():
            result.update(_collect_asset_ids(child))
        return result
    return set()


def _atomic_write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def _write_portal_metadata(outputs: dict[str, Path]) -> dict[str, Path]:
    written: dict[str, Path] = {}
    for slug, (label, description, source_ids) in PORTAL_INPUTS.items():
        directory = PORTAL_CONFIG_ROOT / slug
        evidence: set[str] = set()
        for name in (
            "timeline.yaml",
            "capability-method-map.yaml",
            "landmarks.yaml",
            "benchmarks.yaml",
            "reading-routes.yaml",
        ):
            value = yaml.safe_load((directory / name).read_text(encoding="utf-8"))
            evidence.update(_collect_asset_ids(value))
        if not evidence:
            raise ValueError(f"{slug}: curated portal has no asset evidence")
        source_inputs: list[dict[str, Any]] = []
        for source_id in source_ids:
            manifest = outputs[source_id]
            stat = manifest.stat()
            source_inputs.append(
                {
                    "path": manifest.relative_to(CORE_ROOT).as_posix(),
                    "sha256": _sha256(manifest),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        metadata = {
            "schema_version": "research-asset-hub.domain-portal-config/v2",
            "slug": slug,
            "label": label,
            "description": description,
            "content_version": "2026-08-31.1",
            "review_status": "reviewed",
            "original_selector": {
                "source_id": "domain-portals-20260831",
                "source_record_id": f"overview:{slug}",
            },
            "source_inputs": source_inputs,
            "section_evidence": {
                section: sorted(evidence) for section in PORTAL_SECTIONS
            },
            "appendices": [],
        }
        destination = directory / "portal.yaml"
        _atomic_write_yaml(destination, metadata)
        written[slug] = destination
    return written


def _remap_value(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_remap_value(child, replacements) for child in value]
    if isinstance(value, dict):
        return {
            key: _remap_value(child, replacements) for key, child in value.items()
        }
    return value


def reconcile_portal_asset_ids(catalog_path: Path) -> dict[str, int]:
    replacements: dict[str, str] = {}
    domains_by_asset: dict[str, set[str]] = {}
    with catalog_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("asset"), dict):
                raise ValueError(f"catalog line {line_number} is not an open-catalog record")
            asset = record["asset"]
            canonical_id = str(asset["asset_id"])
            domains_by_asset[canonical_id] = set(asset.get("domains") or [])
            provenance = record.get("provenance")
            source_records = (
                provenance.get("source_records", [])
                if isinstance(provenance, dict)
                else []
            )
            for source_record in source_records:
                if not isinstance(source_record, dict):
                    continue
                source_id = source_record.get("source_alias")
                source_record_id = source_record.get("source_record_id")
                if source_id not in PAPER_SOURCE_IDS or not isinstance(
                    source_record_id, str
                ):
                    continue
                candidate_id = str(stable_uuid7(str(source_id), source_record_id))
                replacements[candidate_id] = canonical_id

    documents: dict[Path, dict[str, Any]] = {}
    changed_by_slug: dict[str, int] = {}
    for slug in PORTAL_INPUTS:
        before_ids: set[str] = set()
        after_ids: set[str] = set()
        for name in PORTAL_YAML_FILES:
            path = PORTAL_CONFIG_ROOT / slug / name
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError(f"portal YAML must be an object: {path}")
            before_ids.update(_collect_asset_ids(document))
            remapped = _remap_value(document, replacements)
            if not isinstance(remapped, dict):
                raise TypeError(f"remapped portal YAML must be an object: {path}")
            after_ids.update(_collect_asset_ids(remapped))
            documents[path] = remapped
        unresolved = before_ids - replacements.keys() - set(replacements.values())
        if unresolved:
            raise ValueError(
                f"{slug}: catalog has no source-record mapping for {sorted(unresolved)}"
            )
        wrong_domain = {
            asset_id
            for asset_id in after_ids
            if slug not in domains_by_asset.get(asset_id, set())
        }
        if wrong_domain:
            raise ValueError(
                f"{slug}: canonical assets are outside the portal domain: "
                f"{sorted(wrong_domain)}"
            )
        changed_by_slug[slug] = sum(
            1
            for asset_id in before_ids
            if asset_id in replacements and replacements[asset_id] != asset_id
        )

    for path, document in documents.items():
        _atomic_write_yaml(path, document)
    return changed_by_slug


def build_all(records_path: Path) -> dict[str, Path]:
    records = _read_records(records_path)
    items_by_source: dict[str, list[dict[str, Any]]] = {
        BASELINE_SOURCE_ID: _baseline_items()
    }
    for source_id, (domain, unit_root, expected) in SOURCE_SPECS.items():
        unit_records = _unit_source_records(unit_root)
        selected = []
        for record in records:
            if domain not in (record.get("domain_slugs") or []):
                continue
            source_record_id = str(record.get("source_record_id") or record.get("paper_id"))
            unit_record = unit_records.get(source_record_id)
            if unit_record is None:
                raise ValueError(f"{source_id}: v2 catalog misses {source_record_id}")
            selected.append(_new_item(record, unit_root, unit_record))
        if len(selected) != expected:
            raise ValueError(f"{source_id}: expected {expected} records, got {len(selected)}")
        if len({item["id"] for item in selected}) != expected:
            raise ValueError(f"{source_id}: duplicate source record IDs")
        items_by_source[source_id] = selected
    items_by_source[ENRICHMENT_SOURCE_ID] = [_enrichment_item(records)]

    # Only start writing after every required PDF/HTML and digest has passed.
    outputs = {
        source_id: _write(source_id, items)
        for source_id, items in items_by_source.items()
    }
    _write_portal_metadata(outputs)
    return outputs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument(
        "--reconcile-catalog",
        type=Path,
        help="after ingest, replace staged UUIDs with canonical open-catalog asset IDs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.reconcile_catalog is not None:
        catalog_path = arguments.reconcile_catalog.expanduser().resolve(strict=True)
        changed = reconcile_portal_asset_ids(catalog_path)
        print(
            json.dumps(
                {"status": "reconciled", "changed_by_slug": changed},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    records_path = arguments.records.expanduser().resolve(strict=True)
    outputs = build_all(records_path)
    print(
        json.dumps(
            {
                "status": "written",
                "sources": {key: str(value) for key, value in sorted(outputs.items())},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
