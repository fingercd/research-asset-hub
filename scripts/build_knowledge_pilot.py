"""Build reproducible, source-bound derivatives without writing the primary database."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pdfplumber
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from apps.api.config import PROJECT_ROOT, ApiSettings
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json, atomic_write_text
from packages.domain.models import Asset, Relation, Representation, Source, SourceRecord
from packages.domain.platform_models import Activity, DocumentChunk
from packages.domain.serving_scope import active_representation_clause
from packages.extraction.html import HTMLExtractor
from packages.knowledge.documents import (
    document_from_chunks,
    export_source_markdown,
    page_evidence_id,
    text_hash,
)

BUILD_VERSION = "page-text-v1"


def scoped_selection(db: Session, sources: list[str], asset_ids: list[str]) -> list[dict[str, str]]:
    """Select existing formal papers, deduplicating overlapping source/asset scopes."""
    items: dict[str, dict[str, str]] = {}
    for alias in dict.fromkeys(sources):
        source = db.scalar(select(Source).where(Source.alias == alias, Source.enabled.is_(True)))
        if source is None:
            raise ValueError(f"Unknown or disabled source: {alias}")
        rows = db.execute(
            select(Asset.id, SourceRecord.source_record_id)
            .join(SourceRecord, SourceRecord.asset_id == Asset.id)
            .where(
                SourceRecord.source_id == source.id,
                Asset.asset_type == "paper",
                Asset.deleted_at.is_(None),
            )
            .order_by(SourceRecord.source_record_id)
        )
        for asset_id, record_id in rows:
            items.setdefault(
                str(asset_id),
                {
                    "asset_id": str(asset_id),
                    "group": alias,
                    "selection_source_id": alias,
                    "source_record_id": record_id,
                },
            )
    for raw_id in asset_ids:
        asset_id = str(UUID(raw_id))
        items.setdefault(asset_id, {"asset_id": asset_id, "group": "explicit-assets"})
    return list(items.values())


def extracted_document(
    db: Session, asset: Asset, rep: Representation, digest: str
) -> dict[str, Any] | None:
    """Reuse verified page chunks rather than re-extract an already processed PDF."""
    chunks = list(
        db.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.asset_id == asset.id,
                DocumentChunk.representation_id == rep.id,
            )
            .order_by(DocumentChunk.chunk_index, DocumentChunk.id)
        )
    )
    if not chunks:
        return None
    for chunk in chunks:
        if chunk.content_hash != text_hash(chunk.text_content):
            raise ValueError("Extracted chunk content fingerprint differs.")
        anchor = chunk.anchor or {}
        if anchor.get("kind") != "pdf" or not anchor.get("page"):
            raise ValueError("Extracted PDF chunk lacks page evidence.")
    for activity_id in {c.activity_id for c in chunks if c.activity_id is not None}:
        activity = db.get(Activity, activity_id)
        paths = {
            (c.anchor or {}).get("source_path") for c in chunks if c.activity_id == activity_id
        }
        if (
            activity is None
            or activity.status != "succeeded"
            or activity.asset_id != asset.id
            or activity.representation_id != rep.id
            or len(paths) != 1
            or None in paths
            or activity.output_digest != text_hash(str(next(iter(paths))) + digest)
        ):
            raise ValueError("Extracted chunks do not match the live PDF fingerprint.")
    if any(c.activity_id is None for c in chunks) and not rep.sha256:
        raise ValueError("Legacy extraction lacks a registered PDF fingerprint.")
    document = document_from_chunks(
        asset_id=str(asset.id),
        representation_id=str(rep.id),
        source_hash=digest,
        title=asset.title,
        source_type="original",
        chunks=[
            {
                "id": str(c.id),
                "chunk_index": c.chunk_index,
                "text_content": c.text_content,
                "anchor": c.anchor,
            }
            for c in chunks
        ],
    )
    document["parser"] = "existing verified document_chunks"
    return document


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def available_representations(db: Session, asset_id: UUID) -> list[Representation]:
    return list(
        db.scalars(
            select(Representation)
            .join(Source, Source.id == Representation.source_id)
            .where(
                Representation.asset_id == asset_id,
                Representation.deleted_at.is_(None),
                Representation.availability == "available",
                Source.enabled.is_(True),
                active_representation_clause(),
            )
            .order_by(Representation.created_at, Representation.id)
        )
    )


def locator(db: Session, rep: Representation) -> dict[str, Any]:
    source = db.get(Source, rep.source_id)
    if source is None or not rep.path_ref:
        raise ValueError("Representation has no registered local source.")
    return {
        "source_id": source.alias,
        "root_key": rep.root_key,
        "path_ref": rep.path_ref,
        "representation_id": str(rep.id),
        "source_hash": rep.sha256,
        "revision": rep.revision,
    }


def resolve(registry: SourceRegistry, reference: dict[str, Any]) -> Path:
    return registry.resolve(reference["source_id"], reference["root_key"], reference["path_ref"])


def analysis_markdown(
    asset: Asset, reps: list[Representation], db: Session, registry: SourceRegistry
) -> tuple[str, dict[str, Any]]:
    intro = (
        f"# {asset.title}\n\n"
        "> 来源类型：已有中文分析，不是论文原文。内容尚未因本次导出重新核验。\n\n"
    )
    if asset.analysis:
        sections = [
            f"## {key}\n\n"
            + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            for key, value in asset.analysis.items()
            if value
        ]
        analysis_hash = hashlib.sha256(
            json.dumps(asset.analysis, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return intro + "\n\n".join(sections) + "\n", {
            "kind": "asset_analysis",
            "content_hash": analysis_hash,
            "review_status": "inherited_unreviewed",
        }
    for rep in reps:
        if rep.representation_type != "html":
            continue
        ref = locator(db, rep)
        path = resolve(registry, ref)
        if not path.is_file():
            continue
        digest = file_hash(path)
        if rep.sha256 and digest != rep.sha256:
            continue
        result = HTMLExtractor().extract(path, representation_id=str(rep.id))
        if result.chunks:
            sections = [f"## {chunk.title or '解读'}\n\n{chunk.text}" for chunk in result.chunks]
            return intro + "\n\n".join(sections) + "\n", {
                "kind": "local_html_analysis",
                **ref,
                "source_hash": digest,
                "review_status": "inherited_unreviewed",
            }
    return intro + "本资产暂缺可导出的中文分析。\n", {"kind": "missing", "review_status": "missing"}


def repository_links(db: Session, asset: Asset, registry: SourceRegistry) -> list[dict[str, Any]]:
    edges = list(
        db.scalars(
            select(Relation).where(
                Relation.subject_asset_id == asset.id,
                Relation.relation_type == "implemented_by",
                Relation.deleted_at.is_(None),
            )
        )
    )
    results: list[dict[str, Any]] = []
    for edge in edges:
        repo = db.get(Asset, edge.object_asset_id)
        if repo is None or repo.deleted_at or repo.attributes.get("system_hidden"):
            continue
        for rep in available_representations(db, repo.id):
            if rep.representation_type != "repository" or not rep.path_ref:
                continue
            ref = locator(db, rep)
            results.append(
                {
                    "asset_id": str(repo.id),
                    "title": repo.title,
                    "relation_status": edge.confirmation_status,
                    "exists": resolve(registry, ref).is_dir(),
                    **ref,
                }
            )
    return results


def build_paper(
    db: Session, registry: SourceRegistry, item: dict[str, str], output: Path
) -> dict[str, Any]:
    asset = db.get(Asset, UUID(item["asset_id"]))
    if asset is None or asset.deleted_at or asset.attributes.get("system_hidden"):
        raise ValueError("Selected asset is missing, deleted or hidden.")
    reps = available_representations(db, asset.id)
    choices = [r for r in reps if r.representation_type == "pdf" and r.path_ref]
    if not choices:
        raise ValueError("Selected asset has no active available PDF.")
    rep = choices[0]
    ref = locator(db, rep)
    path = resolve(registry, ref)
    digest = file_hash(path)
    if rep.sha256 and rep.sha256 != digest:
        raise ValueError("Registered PDF fingerprint differs from current file.")
    ref["source_hash"] = digest
    directory = output / "v1" / str(asset.id)
    document_path = directory / "document.json"
    complete = all(
        (directory / name).is_file()
        for name in ("document.json", "source.md", "analysis.zh.md", "metadata.json")
    )
    if document_path.exists():
        cached = json.loads(document_path.read_text(encoding="utf-8"))
        if (
            cached.get("source_hash") != digest
            or cached.get("representation_id") != str(rep.id)
            or cached.get("builder_version") != BUILD_VERSION
        ):
            raise ValueError("Existing derivative version differs; create a new version.")
        if complete:
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            if metadata.get("source_hash") != digest or metadata.get("representation_id") != str(
                rep.id
            ):
                raise ValueError("Existing derivative metadata differs from its PDF.")
            return {**metadata, "build_action": "reused"}
        pages = cached["pages"]
        warnings = cached["warnings"]
    else:
        extracted = extracted_document(db, asset, rep, digest)
        pages = extracted["pages"] if extracted else []
        warnings = []
        if extracted is None:
            with pdfplumber.open(path) as document:
                for index, page in enumerate(document.pages):
                    content = page.extract_text() or ""
                    page_number = index + 1
                    if len(content.strip()) < 80:
                        warnings.append(
                            f"Page {page_number}: low text density; visual review/OCR needed."
                        )
                    pages.append(
                        {
                            "page": page_number,
                            "text": content,
                            "evidence_id": page_evidence_id(
                                str(rep.id), digest, page_number, text_hash(content)
                            ),
                            "content_hash": text_hash(content),
                            "anchor": {"kind": "pdf", "page": page_number, "precision": "page"},
                        }
                    )
        atomic_write_json(
            document_path,
            {
                "schema_version": 1,
                "builder_version": BUILD_VERSION,
                "asset_id": str(asset.id),
                "representation_id": str(rep.id),
                "title": asset.title,
                "source_hash": digest,
                "source_locator": ref,
                "parser": (
                    extracted["parser"] if extracted else f"pdfplumber {pdfplumber.__version__}"
                ),
                "source_type": "original",
                "extraction_reused": extracted is not None,
                "quality_status": "page_text_unreviewed",
                "fidelity": "page text; formulas, table structure and reading order require review",
                "pages": pages,
                "warnings": warnings,
            },
        )
    document_data = json.loads(document_path.read_text(encoding="utf-8"))
    if not (directory / "source.md").exists():
        atomic_write_text(
            directory / "source.md",
            "> 原文文本抽取；公式、表格和双栏阅读顺序仍需复核。\n\n"
            + export_source_markdown(document_data),
        )
    analysis, analysis_source = analysis_markdown(asset, reps, db, registry)
    if not (directory / "analysis.zh.md").exists():
        atomic_write_text(directory / "analysis.zh.md", analysis)
    repositories = repository_links(db, asset, registry)
    meta = {
        "schema_version": 1,
        **item,
        "title": asset.title,
        "domains": asset.domain_slugs,
        "year": asset.year,
        **ref,
        "page_count": len(pages),
        "pdf_candidates": len(choices),
        "selection_policy": "oldest active available PDF; explicit representation ID retained",
        "analysis_source": analysis_source,
        "repositories": repositories,
        "quality_status": "page_text_unreviewed",
        "warnings": warnings,
        "extraction_reused": document_data.get("extraction_reused", False),
    }
    if not (directory / "metadata.json").exists():
        atomic_write_json(directory / "metadata.json", meta)
    return {**meta, "build_action": "built"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path)
    parser.add_argument(
        "--source", action="append", default=[], help="Formal source alias; repeatable"
    )
    parser.add_argument(
        "--asset-id", action="append", default=[], help="Existing asset UUID; repeatable"
    )
    parser.add_argument(
        "--report", type=Path, help="Scoped report; existing pilot manifest is preserved"
    )
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs/knowledge-pilot")
    args = parser.parse_args()
    scoped = bool(args.source or args.asset_id)
    if scoped and args.selection:
        parser.error("Use either --selection or --source/--asset-id.")
    selection = (
        {}
        if scoped
        else json.loads(
            (args.selection or PROJECT_ROOT / "config/knowledge-pilot.json").read_text(
                encoding="utf-8"
            )
        )
    )
    output = args.output.resolve()
    if not output.is_relative_to(PROJECT_ROOT / "outputs"):
        parser.error("Pilot derivatives must remain inside platform outputs.")
    report_path = args.report or output / ("incremental.json" if scoped else "pilot.json")
    report_path = report_path.resolve()
    if not any(
        report_path.is_relative_to(PROJECT_ROOT / folder)
        for folder in ("outputs", "work/validation")
    ):
        parser.error("Reports must remain inside platform outputs or work/validation.")
    settings = ApiSettings()
    registry = SourceRegistry.load(settings.sources_config_path)
    engine = create_engine(
        settings.database_url.replace("postgresql://", "postgresql+psycopg://"),
        connect_args={"connect_timeout": 5},
    )
    papers, failures = [], []
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '30s'"))
        try:
            items = (
                scoped_selection(db, args.source, args.asset_id) if scoped else selection["papers"]
            )
        except ValueError as error:
            parser.error(str(error))
        ids = [p["asset_id"] for p in items]
        if len(set(ids)) != len(ids):
            parser.error("Selection contains duplicate assets.")
        for item in items:
            try:
                result = build_paper(db, registry, item, output)
                papers.append(result)
                print(
                    f"{result['build_action']} {len(papers)}/{len(ids)}: {result['title']}",
                    flush=True,
                )
            except (OSError, ValueError, RuntimeError) as error:
                failures.append({"asset_id": item["asset_id"], "reason": str(error)})
                print(f"failed {item['asset_id']}: {error}", flush=True)
    report = {
        "schema_version": 1,
        "pilot_id": "incremental" if scoped else selection["pilot_id"],
        "selection": {"sources": args.source, "asset_ids": args.asset_id},
        "generated_at": datetime.now(UTC).isoformat(),
        "primary_database_read_only": True,
        "source_files_modified": 0,
        "selected_count": len(ids),
        "completed_count": len(papers),
        "built_count": sum(p["build_action"] == "built" for p in papers),
        "reused_count": sum(p["build_action"] == "reused" for p in papers),
        "page_count": sum(p["page_count"] for p in papers),
        "groups": dict(Counter(p["group"] for p in papers)),
        "papers": papers,
        "failures": failures,
    }
    atomic_write_json(report_path, report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"papers"}}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
