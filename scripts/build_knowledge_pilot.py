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

import pymupdf
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from apps.api.config import PROJECT_ROOT, ApiSettings
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json, atomic_write_text
from packages.domain.models import Asset, Relation, Representation, Source
from packages.domain.serving_scope import active_representation_clause
from packages.extraction.html import HTMLExtractor
from packages.knowledge.documents import export_source_markdown, page_evidence_id, text_hash

BUILD_VERSION = "page-text-v1"


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
    if document_path.exists():
        cached = json.loads(document_path.read_text(encoding="utf-8"))
        if cached.get("source_hash") != digest or cached.get("builder_version") != BUILD_VERSION:
            raise ValueError("Existing derivative version differs; create a new version.")
        # Refresh exported analysis from its own input; never replace source/figure reviews.
        pages = cached["pages"]
        warnings = cached["warnings"]
    else:
        pages = []
        warnings = []
        with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
            for index, page in enumerate(document):
                content = page.get_text("text", sort=True)
                page_number = index + 1
                if len(content.strip()) < 80:
                    warnings.append(
                        f"Page {page_number}: low text density; visual review/OCR needed."
                    )
                evidence_id = f"{rep.id}-p{page_number}-{digest[:12]}"
                pages.append(
                    {
                        "page": page_number,
                        "text": content,
                        "evidence_id": evidence_id,
                        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
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
                "parser": f"PyMuPDF {pymupdf.VersionBind}",
                "quality_status": "page_text_unreviewed",
                "fidelity": "page text; formulas, table structure and reading order require review",
                "pages": pages,
                "warnings": warnings,
            },
        )
    # Use the same evidence identities as the serving API and methodology cards.
    document_data = json.loads(document_path.read_text(encoding="utf-8"))
    for page in document_data["pages"]:
        page["evidence_id"] = page_evidence_id(
            str(rep.id), digest, page["page"], text_hash(page["text"])
        )
    atomic_write_json(document_path, document_data)
    atomic_write_text(
        directory / "source.md",
        "> 原文文本抽取；公式、表格和双栏阅读顺序仍需复核。\n\n"
        + export_source_markdown(document_data),
    )
    analysis, analysis_source = analysis_markdown(asset, reps, db, registry)
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
    }
    atomic_write_json(directory / "metadata.json", meta)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selection", type=Path, default=PROJECT_ROOT / "config/knowledge-pilot.json"
    )
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs/knowledge-pilot")
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    ids = [p["asset_id"] for p in selection["papers"]]
    if len(set(ids)) != len(ids):
        parser.error("Pilot selection contains duplicate assets.")
    output = args.output.resolve()
    if not output.is_relative_to(PROJECT_ROOT / "outputs"):
        parser.error("Pilot derivatives must remain inside platform outputs.")
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
        for item in selection["papers"]:
            try:
                result = build_paper(db, registry, item, output)
                papers.append(result)
                print(f"built {len(papers)}/{len(ids)}: {result['title']}", flush=True)
            except (OSError, ValueError, RuntimeError) as error:
                failures.append({"asset_id": item["asset_id"], "reason": str(error)})
                print(f"failed {item['asset_id']}: {error}", flush=True)
    report = {
        "schema_version": 1,
        "pilot_id": selection["pilot_id"],
        "generated_at": datetime.now(UTC).isoformat(),
        "primary_database_read_only": True,
        "source_files_modified": 0,
        "selected_count": len(ids),
        "built_count": len(papers),
        "page_count": sum(p["page_count"] for p in papers),
        "groups": dict(Counter(p["group"] for p in papers)),
        "papers": papers,
        "failures": failures,
    }
    atomic_write_json(output / "pilot.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"papers"}}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
