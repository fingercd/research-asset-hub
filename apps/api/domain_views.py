"""Public domain groupings over immutable source classifications."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from packages.domain.models import Asset
from packages.domain.platform_models import SourceRecordAsset


def presentation_domains(asset: Any) -> list[str]:
    """Keep source slugs intact while presenting the requested topic boundaries."""
    assigned = getattr(asset, "_presentation_domains", None)
    if assigned is not None:
        return list(assigned)
    domains = list(asset.domain_slugs or [])
    if "navigation-compression" in domains:
        domains.remove("navigation-compression")
        if "embodied-ai-compression" not in domains:
            domains.append("embodied-ai-compression")
    if "generation" in domains:
        domains.remove("generation")
        category = str((asset.attributes or {}).get("category") or "")
        title = str(asset.title or "").casefold()
        tags = {str(tag).casefold() for tag in (asset.tags or [])}
        video = (
            category.startswith("video_")
            or (category == "survey" and "video" in title)
            or "video-deleted-review" in tags
            or ("video" in title and category not in {"image_core", "image_supplement"})
        )
        target = "video-generation" if video else "image-editing"
        if target not in domains:
            domains.append(target)
    return domains


def annotate_presentation_domains(session: Session, assets: list[Any]) -> None:
    """Assign generation ORM assets or card projections their source-paper topics."""
    by_id = {asset.id: asset for asset in assets}
    repository_ids = {
        asset.id
        for asset in assets
        if asset.asset_type == "repository" and "generation" in (asset.domain_slugs or [])
    }
    if not repository_ids:
        return
    paper_domains: dict[Any, set[str]] = {repository_id: set() for repository_id in repository_ids}
    source_rows = session.execute(
        select(
            SourceRecordAsset.source_record_id,
            SourceRecordAsset.asset_id,
        ).where(
            SourceRecordAsset.asset_id.in_(by_id),
            SourceRecordAsset.deleted_at.is_(None),
        )
    ).all()
    papers_by_source = {
        source_record_id: by_id[asset_id]
        for source_record_id, asset_id in source_rows
        if asset_id in by_id and by_id[asset_id].asset_type == "paper"
    }
    missing_keys = {
        source_record_id
        for source_record_id, asset_id in source_rows
        if asset_id in repository_ids and source_record_id not in papers_by_source
    }
    if missing_keys:
        for source_record_id, paper in session.execute(
            select(SourceRecordAsset.source_record_id, Asset)
            .join(Asset, Asset.id == SourceRecordAsset.asset_id)
            .options(
                load_only(
                    Asset.id,
                    Asset.asset_type,
                    Asset.title,
                    Asset.domain_slugs,
                    Asset.attributes,
                    Asset.tags,
                )
            )
            .where(
                SourceRecordAsset.source_record_id.in_(missing_keys),
                SourceRecordAsset.deleted_at.is_(None),
                Asset.asset_type == "paper",
            )
        ):
            papers_by_source[source_record_id] = paper
    for source_record_id, asset_id in source_rows:
        if asset_id not in repository_ids:
            continue
        paper = papers_by_source.get(source_record_id)
        if paper is not None:
            paper_domains[asset_id].update(
                domain
                for domain in presentation_domains(paper)
                if domain in {"image-editing", "video-generation"}
            )
    for repository_id, domains in paper_domains.items():
        if domains:
            repository = by_id[repository_id]
            original = [
                domain for domain in repository.domain_slugs or [] if domain != "generation"
            ]
            cast(Any, repository)._presentation_domains = [*original, *sorted(domains)]
