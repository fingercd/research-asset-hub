"""Prepare source-bound original abstracts; apply only via audited HTTP overrides.

The default is read-only collection. Source manifests, PDFs and source claims
are never modified. Only the first three cached/extracted PDF pages are read.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pdfplumber
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import create_database_engine
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Asset, Representation, Source, SourceRecord
from packages.domain.platform_models import DocumentChunk
from packages.domain.serving_scope import active_representation_clause
from packages.knowledge.abstracts import (
    extract_abstract,
    normalize_pdf_text,
    parse_arxiv_abstract,
    parse_proceedings_abstract,
    title_identity,
)

DEFAULT_OUTPUT = (
    PROJECT_ROOT / "work/validation/metadata-knowledge-20261002/abstract-candidates.json"
)
CORRECTED_OFFICIAL_PDFS = {
    "TASO: Optimizing Deep Learning Computation with Automatic Generation of Graph Substitutions": "https://www.cs.cmu.edu/~zhihaoj2/papers/sosp19.pdf",
    "Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations": "https://www.eecs.harvard.edu/~htk/publication/2019-mapl-tillet-kung-cox.pdf",
    "Alibaba HPN: A Data Center Network for Large Language Model Training": "https://ennanzhai.github.io/pub/sigcomm24-hpn.pdf",
    "A Generic Communication Scheduler for Distributed DNN Training Acceleration（ByteScheduler）": "https://i.cs.hku.hk/~cwu/papers/yhpeng-sosp19.pdf",
    "Large Scale Distributed Deep Networks": "https://papers.neurips.cc/paper/4687-large-scale-distributed-deep-networks.pdf",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _arxiv_urls(value: Any) -> list[str]:
    """Use registered URLs/identifiers only; do not run broad title searches."""
    serialized = json.dumps(value, ensure_ascii=False)
    ids = re.findall(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", serialized)

    def identifier_values(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("scheme") == "arxiv":
                ids.append(node["value"])
            for key in ("arxiv_id", "arxiv"):
                value = node.get(key)
                if isinstance(value, str) and re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", value):
                    ids.append(value)
            for child in node.values():
                identifier_values(child)
        elif isinstance(node, list):
            for child in node:
                identifier_values(child)

    identifier_values(value)
    return list(dict.fromkeys(f"https://arxiv.org/abs/{identifier}" for identifier in ids))


def _proceedings_urls(value: Any) -> list[str]:
    domains = {
        "proceedings.neurips.cc",
        "papers.nips.cc",
        "openaccess.thecvf.com",
        "proceedings.mlr.press",
        "aclanthology.org",
        "www.nature.com",
        "nature.com",
        "link.springer.com",
        "link.springernature.com",
        "www.science.org",
        "www.usenix.org",
    }
    urls = re.findall(r"https?://[^\s\"<>]+", json.dumps(value))
    return list(
        dict.fromkeys(
            url
            for url in urls
            if urlsplit(url).hostname in domains and not urlsplit(url).path.endswith(".pdf")
        )
    )


def supplement_official(
    report: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    cache_dir: Path,
) -> dict[str, Any]:
    """Fill local failures from labelled official metadata with exact title checks.

    Each URL is cached as a small metadata evidence record; remote PDF bytes stay
    in memory. No registered source file or full-paper Markdown is created.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    unresolved = []
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        for index, item in enumerate(report["unresolved"]):
            evidence = (catalog or {}).get(item["asset_id"], [])
            urls = _arxiv_urls([item, evidence]) + _proceedings_urls([item, evidence])
            corrected = CORRECTED_OFFICIAL_PDFS.get(item["title"])
            if corrected:
                urls.insert(0, corrected)
            reasons = list(item.get("reasons", []))
            candidate = None
            for url in urls:
                cache_path = cache_dir / f"{digest(url)}.json"
                source = None
                if cache_path.exists():
                    source = json.loads(cache_path.read_text(encoding="utf-8"))
                    if (
                        source.get("kind") == "official_proceedings_author_abstract"
                        and not source.get("title")
                        and source.get("metadata_parser_version") != 3
                    ):
                        source = None
                if source is None:
                    try:
                        response = client.get(url)
                        response.raise_for_status()
                        source = {
                            "requested_url": url,
                            "resolved_url": str(response.url),
                            "response_sha256": hashlib.sha256(response.content).hexdigest(),
                            "retrieved_at": datetime.now(UTC).isoformat(),
                            "metadata_parser_version": 3,
                        }
                        if url == corrected:
                            with pdfplumber.open(
                                io.BytesIO(response.content), pages=[1]
                            ) as pdf:
                                first = pdf.pages[0].extract_text() or ""
                            excerpt, reason = extract_abstract(first)
                            source.update(
                                kind="official_author_pdf_labelled_abstract",
                                title=first.split("\n")[0],
                                first_page_header=title_identity(item["title"], first),
                                abstract=excerpt.text if excerpt else None,
                                reason=reason,
                                page=1,
                                text_input_sha256=digest(first),
                            )
                        else:
                            is_arxiv = urlsplit(url).hostname == "arxiv.org"
                            parsed = (
                                parse_arxiv_abstract(response.text)
                                if is_arxiv
                                else parse_proceedings_abstract(response.text)
                            )
                            source.update(
                                kind="official_arxiv_author_abstract"
                                if is_arxiv
                                else "official_proceedings_author_abstract",
                                title=parsed[0] if parsed else None,
                                abstract=parsed[1] if parsed else None,
                            )
                        cache_path.write_text(
                            json.dumps(source, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as error:
                        reasons.append("official_fetch:" + type(error).__name__)
                        continue
                identity = source.get("first_page_header") or title_identity(
                    item["title"], source.get("title") or ""
                )
                if identity["status"] != "verified":
                    reasons.append("official_title_unverified:" + url)
                    report.setdefault("identity_rejections", []).append(
                        {**item, "input": "official_source", "url": url, "identity": identity}
                    )
                    continue
                original = source.get("abstract")
                if not original:
                    reasons.append("official_labelled_abstract_missing:" + url)
                    continue
                candidate = {
                    **{key: value for key, value in item.items() if key != "reasons"},
                    "abstract": original,
                    "language": "en",
                    "abstract_sha256": digest(original),
                    "confidence": "verified_source_labelled_original",
                    "provenance": {
                        **{key: value for key, value in source.items() if key != "abstract"},
                        "identity": identity,
                        "evidence_cache": str(cache_path.relative_to(PROJECT_ROOT)),
                        "registered_catalog_links": [
                            {key: value for key, value in entry.items() if key != "record"}
                            for entry in evidence
                        ],
                    },
                }
                break
            if candidate:
                report["candidates"].append(candidate)
            else:
                unresolved.append(
                    {**item, "reasons": sorted(set(reasons)) or ["no_official_abstract_url"]}
                )
            if index % 10 == 0:
                print(
                    f"official abstract progress: {index + 1}/{len(report['unresolved'])}",
                    flush=True,
                )
    report["unresolved"] = unresolved
    report["counts"].update(
        candidates=len(report["candidates"]),
        unresolved=len(unresolved),
        candidate_sources=dict(
            Counter(item["provenance"]["kind"] for item in report["candidates"])
        ),
    )
    return report


def explicit_abstracts(value: Any, pointer: str = "") -> list[tuple[str, str]]:
    """Reuse only fields explicitly named abstract, not summary/analysis/guide."""
    results: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{pointer}/{key}"
            # Several frozen catalogs use abstract_zh for an authored Chinese
            # guide. Its name is not evidence of an original author abstract.
            if key.casefold() in {"abstract", "abstract_en"}:
                if isinstance(item, str) and len(item.strip()) >= 120:
                    results.append((child, item.strip()))
            elif key not in {"analysis", "analyses", "guide", "code", "abstract_zh"}:
                results.extend(explicit_abstracts(item, child))
    return results


def collect_candidates(*, pdf_fallback: bool = True) -> dict[str, Any]:
    settings = get_settings()
    registry = SourceRegistry.load(settings.sources_config_path)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "scope": "visible registered paper assets missing abstract",
        "normalization": "NFKC, soft-hyphen removal, line-end word dehyphenation, whitespace",
        "candidates": [],
        "unresolved": [],
        "identity_rejections": [],
    }
    engine = create_database_engine(settings)
    with Session(engine) as session:
        # Read-only PostgreSQL transaction is an additional guard against accidental writes.
        if engine.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        papers = [
            asset
            for asset in session.scalars(
                select(Asset).where(Asset.asset_type == "paper", Asset.deleted_at.is_(None))
            ).all()
            if not asset.attributes.get("system_hidden")
        ]
        missing = [asset for asset in papers if not (asset.abstract or "").strip()]
        report["counts"] = {"visible_papers": len(papers), "missing_before": len(missing)}
        ids = [asset.id for asset in missing]
        aliases = {source.id: source.alias for source in session.scalars(select(Source))}
        records: dict[Any, list[SourceRecord]] = defaultdict(list)
        for record in session.scalars(select(SourceRecord).where(SourceRecord.asset_id.in_(ids))):
            records[record.asset_id].append(record)
        reps: dict[Any, list[Representation]] = defaultdict(list)
        for rep in session.scalars(
            select(Representation)
            .where(
                Representation.asset_id.in_(ids),
                Representation.representation_type == "pdf",
                Representation.availability == "available",
                Representation.deleted_at.is_(None),
                active_representation_clause(),
            )
            .order_by(Representation.created_at.desc(), Representation.id)
        ):
            reps[rep.asset_id].append(rep)
        chunks: dict[Any, list[DocumentChunk]] = defaultdict(list)
        for chunk in session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.asset_id.in_(ids), DocumentChunk.chunk_index < 12)
            .order_by(DocumentChunk.representation_id, DocumentChunk.chunk_index)
        ):
            if int((chunk.anchor or {}).get("page", 9999)) <= 3:
                chunks[chunk.representation_id].append(chunk)
        for asset in missing:
            base = {
                "asset_id": str(asset.id),
                "title": asset.title,
                "row_version": asset.row_version,
                "domains": asset.domain_slugs,
                "source_urls": asset.source_urls,
                "external_identifiers": [
                    {"scheme": identifier.scheme, "value": identifier.value}
                    for identifier in asset.external_identifiers
                ],
            }
            result: dict[str, Any] | None = None
            reasons: list[str] = []
            for record in records[asset.id]:
                originals = explicit_abstracts(record.raw_data)
                if originals:
                    record_title = record.source_fields.get("title") or record.raw_data.get(
                        "title", ""
                    )
                    identity = title_identity(asset.title, str(record_title))
                    if identity["status"] != "verified":
                        reasons.append("source_record_title_unverified")
                        continue
                    pointer, original = originals[0]
                    result = {
                        "abstract": original,
                        "language": "zh"
                        if any("\u4e00" <= char <= "\u9fff" for char in original)
                        else "en",
                        "provenance": {
                            "kind": "source_record_original_abstract",
                            "source_alias": aliases[record.source_id],
                            "source_record_id": record.source_record_id,
                            "source_record_uuid": str(record.id),
                            "source_record_digest": record.normalized_digest,
                            "json_pointer": "/raw_data" + pointer,
                            "original_value_sha256": digest(original),
                            "identity": identity,
                        },
                    }
                    break
            for rep in reps[asset.id] if result is None else []:
                provenance: dict[str, Any] = {
                    "representation_id": str(rep.id),
                    "source_alias": aliases.get(rep.source_id) if rep.source_id else None,
                    "root_key": rep.root_key,
                    "path_ref": rep.path_ref,
                    "registered_pdf_sha256": rep.sha256,
                }
                saved = chunks[rep.id]
                # Keep only one parser version; duplicate chunks corrupt the reading order.
                if saved:
                    parser = (saved[0].parser_name, saved[0].parser_version)
                    saved = [
                        chunk
                        for chunk in saved
                        if (chunk.parser_name, chunk.parser_version) == parser
                    ]
                    cached_text = "\n".join(chunk.text_content for chunk in saved)
                    first_page = "\n".join(
                        chunk.text_content
                        for chunk in saved
                        if (chunk.anchor or {}).get("page") == 1
                    )
                    identity = title_identity(asset.title, first_page)
                    if identity["status"] != "verified":
                        report["identity_rejections"].append(
                            {**base, **provenance, "input": "cached_pdf", "identity": identity}
                        )
                        excerpt, reason = None, "first_page_title_unverified"
                    else:
                        excerpt, reason = extract_abstract(cached_text)
                    if excerpt:
                        result = {
                            "abstract": excerpt.text,
                            "language": excerpt.language,
                            "provenance": {
                                **provenance,
                                "kind": "cached_pdf_labelled_abstract",
                                "identity": identity,
                                "pages_read": sorted(
                                    {(chunk.anchor or {}).get("page") for chunk in saved}
                                ),
                                "chunks": [
                                    {
                                        "id": str(chunk.id),
                                        "content_hash": chunk.content_hash,
                                        "anchor": chunk.anchor,
                                    }
                                    for chunk in saved
                                ],
                                "text_input_sha256": digest(cached_text),
                                "excerpt_start": excerpt.start,
                                "excerpt_end": excerpt.end,
                                "end_marker": excerpt.end_marker,
                            },
                        }
                        break
                    reasons.append("cached:" + reason)
                if pdf_fallback and rep.path_ref and provenance["source_alias"]:
                    try:
                        path = registry.resolve(
                            str(provenance["source_alias"]), rep.root_key, rep.path_ref
                        )
                        current_sha = file_digest(path)
                        if rep.sha256 and current_sha != rep.sha256:
                            reasons.append("pdf_source_hash_changed")
                            continue
                        with pdfplumber.open(path, pages=[1, 2, 3]) as pdf:
                            # Native page text, at most three pages; no full-paper extraction.
                            pages_read = len(pdf.pages)
                            page_texts = [page.extract_text() or "" for page in pdf.pages]
                            page_text = "\n".join(page_texts)
                        identity = title_identity(asset.title, page_texts[0])
                        if identity["status"] != "verified":
                            report["identity_rejections"].append(
                                {
                                    **base,
                                    **provenance,
                                    "input": "native_pdf",
                                    "verified_pdf_sha256": current_sha,
                                    "identity": identity,
                                }
                            )
                            excerpt, reason = None, "first_page_title_unverified"
                        else:
                            excerpt, reason = extract_abstract(page_text)
                        if excerpt:
                            result = {
                                "abstract": excerpt.text,
                                "language": excerpt.language,
                                "provenance": {
                                    **provenance,
                                    "kind": "pdf_labelled_abstract",
                                    "identity": identity,
                                    "verified_pdf_sha256": current_sha,
                                    "pages_read": list(range(1, pages_read + 1)),
                                    "text_input_sha256": digest(page_text),
                                    "excerpt_start": excerpt.start,
                                    "excerpt_end": excerpt.end,
                                    "end_marker": excerpt.end_marker,
                                },
                            }
                            break
                        reasons.append("pdf:" + reason)
                    except (OSError, ValueError, RuntimeError) as error:
                        reasons.append("pdf_read:" + type(error).__name__)
            if result:
                result["abstract_sha256"] = digest(result["abstract"])
                result["confidence"] = "source_labelled_original_requires_review"
                report["candidates"].append({**base, **result})
            else:
                report["unresolved"].append(
                    {
                        **base,
                        "reasons": sorted(set(reasons))
                        or ["no_active_pdf_or_saved_author_abstract"],
                    }
                )
    report["counts"].update(
        {
            "candidates": len(report["candidates"]),
            "unresolved": len(report["unresolved"]),
            "candidate_sources": dict(
                Counter(item["provenance"]["kind"] for item in report["candidates"])
            ),
        }
    )
    return report


def apply_candidates(report: dict[str, Any], *, api: str) -> list[dict[str, Any]]:
    """Idempotent overrides with the current asset ETag; skip concurrent changes."""
    outcomes: list[dict[str, Any]] = []
    with httpx.Client(base_url=api.rstrip("/"), timeout=120) as client:
        # This feature gate prevents silently applying against an old API whose
        # override endpoint ignores If-Match and cannot protect concurrent edits.
        schema = client.get("/openapi.json")
        schema.raise_for_status()
        parameters = schema.json()["paths"]["/api/v1/overrides"]["post"].get("parameters", [])
        if not any(item.get("name") == "If-Match" for item in parameters):
            raise RuntimeError("Override API lacks asset If-Match protection; apply refused.")
        for candidate in report["candidates"]:
            asset_id = candidate["asset_id"]
            if candidate["provenance"].get("identity", {}).get("status") != "verified":
                raise ValueError(f"Candidate lacks verified source title identity: {asset_id}")
            current = client.get(f"/api/v1/assets/{asset_id}")
            current.raise_for_status()
            asset = current.json()
            state = {"asset_id": asset_id}
            if asset["id"] != asset_id or asset["title"] != candidate["title"]:
                outcomes.append({**state, "status": "identity_changed_skip"})
                continue
            if (asset.get("abstract") or "").strip():
                status = (
                    "already_present"
                    if normalize_pdf_text(asset["abstract"])
                    == (normalize_pdf_text(candidate["abstract"]))
                    else "nonempty_abstract_skip"
                )
                outcomes.append({**state, "status": status})
                continue
            if asset["row_version"] != candidate["row_version"]:
                outcomes.append({**state, "status": "row_version_changed_skip"})
                continue
            provenance = candidate["provenance"]
            rep_id = provenance.get("representation_id")
            if rep_id:
                rep = next(
                    (value for value in asset["representations"] if value["id"] == rep_id), None
                )
                if rep is None or rep.get("sha256") != provenance["registered_pdf_sha256"]:
                    outcomes.append({**state, "status": "representation_changed_skip"})
                    continue
            value_sha = digest(candidate["abstract"])
            if value_sha != candidate["abstract_sha256"]:
                raise ValueError(f"Candidate abstract hash mismatch: {asset_id}")
            reason = json.dumps(
                {
                    "purpose": (
                        "补齐官方模型卡或系统卡介绍；不是论文作者摘要"
                        if provenance.get("is_paper_abstract") is False
                        else "补齐作者原始摘要；不替代中文导读"
                    ),
                    "abstract_language": candidate["language"],
                    "abstract_sha256": value_sha,
                    "provenance": provenance,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            response = client.post(
                "/api/v1/overrides",
                headers={
                    "If-Match": current.headers["ETag"],
                    "Idempotency-Key": f"abstract-enrichment:{asset_id}:{value_sha}",
                },
                json={
                    "entity_type": "asset",
                    "entity_id": asset_id,
                    "field_path": "/abstract",
                    "override_value": candidate["abstract"],
                    "reason": reason,
                },
            )
            if response.status_code in {409, 412}:
                outcomes.append({**state, "status": "concurrent_conflict_skip"})
                continue
            response.raise_for_status()
            outcomes.append({**state, "status": "applied", "override_id": response.json()["id"]})
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-pdf-fallback", action="store_true")
    parser.add_argument("--official-fallback", action="store_true")
    parser.add_argument("--supplement", type=Path, help="Supplement an existing collection report")
    parser.add_argument("--catalog", type=Path, help="Read-only registered catalog link evidence")
    parser.add_argument("--apply", type=Path, help="Apply this reviewed candidate report via HTTP")
    parser.add_argument("--api", default="http://127.0.0.1:8767")
    args = parser.parse_args()
    report: dict[str, Any]
    if args.apply:
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "outcomes": apply_candidates(
                json.loads(args.apply.read_text(encoding="utf-8")), api=args.api
            ),
        }
    else:
        report = (
            json.loads(args.supplement.read_text(encoding="utf-8"))
            if args.supplement
            else collect_candidates(pdf_fallback=not args.no_pdf_fallback)
        )
        if args.official_fallback:
            report = supplement_official(
                report,
                catalog=json.loads(args.catalog.read_text(encoding="utf-8"))
                if args.catalog
                else None,
                cache_dir=DEFAULT_OUTPUT.parent / "abstract-official-cache",
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            report.get(
                "counts", dict(Counter(item["status"] for item in report.get("outcomes", [])))
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
