"""Generate the unified, open export tree without copying source binaries."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.exporters.common import atomic_write_json, atomic_write_text
from packages.domain.models import (
    Annotation,
    Asset,
    ExternalIdentifierModel,
    FieldClaim,
    FieldResolution,
    ReadingState,
    Relation,
    RelationEvidenceModel,
    Representation,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRecordVersion,
)

EXPORT_SCHEMA = "research-asset-hub.open-catalog/v1"


def _asset_payload(asset: Asset) -> dict[str, Any]:
    return {
        "schema": "research-asset-hub.asset/v1",
        "asset_id": str(asset.id),
        "asset_type": asset.asset_type,
        "title": asset.title,
        "title_zh": asset.title_zh,
        "subtitle": asset.subtitle,
        "authors": asset.authors,
        "year": asset.year,
        "venue": asset.venue,
        "language": asset.language,
        "abstract": asset.abstract,
        "summary": asset.summary,
        "analysis": asset.analysis,
        "visibility": asset.visibility,
        "source_urls": asset.source_urls,
        "domains": asset.domain_slugs,
        "tags": asset.tags,
        "attributes": asset.attributes,
        "row_version": asset.row_version,
        "deleted_at": asset.deleted_at.isoformat() if asset.deleted_at else None,
    }


def _markdown_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_note(document: dict[str, Any]) -> str:
    asset = document["asset"]
    state = document["reading_state"]
    annotations = document["annotations"]
    lines = [
        "---",
        "schema: research-asset-hub.asset/v1",
        f"asset_id: {_markdown_scalar(asset['asset_id'])}",
        f"asset_type: {_markdown_scalar(asset['asset_type'])}",
        f"title: {_markdown_scalar(asset['title'])}",
        f"authors: {_markdown_scalar(asset['authors'])}",
        f"year: {_markdown_scalar(asset['year'])}",
        f"venue: {_markdown_scalar(asset['venue'])}",
        f"domains: {_markdown_scalar(asset['domains'])}",
        f"tags: {_markdown_scalar(asset['tags'])}",
        f"visibility: {_markdown_scalar(asset['visibility'])}",
        f"reading_status: {_markdown_scalar(state['status'] if state else 'unread')}",
        "---",
        "",
        f"# {asset['title_zh'] or asset['title']}",
        "",
    ]
    if asset["title_zh"] and asset["title_zh"] != asset["title"]:
        lines.extend([f"**原题：** {asset['title']}", ""])
    if asset["authors"]:
        lines.extend([f"**作者：** {', '.join(asset['authors'])}", ""])
    if asset["year"] or asset["venue"]:
        publication = " · ".join(
            value for value in [str(asset["year"] or ""), asset["venue"] or ""] if value
        )
        lines.extend([f"**发表：** {publication}", ""])
    summary = asset["abstract"] or asset["summary"]
    lines.extend(["## 摘要", "", summary or "无", ""])

    lines.extend(["## 表示", ""])
    if document["representations"]:
        for item in document["representations"]:
            location = (
                f"{item['source_alias']}:{item['root_key']}/{item['path_ref']}"
                if item["path_ref"]
                else item["external_url"]
            )
            lines.append(
                f"- **{item['representation_type']}**（{item['availability']}）"
                f" `{location}`"
            )
    else:
        lines.append("- 无")
    lines.append("")

    lines.extend(["## 关系", ""])
    if document["relations"]:
        for item in document["relations"]:
            direction = "→" if item["subject_asset_id"] == asset["asset_id"] else "←"
            other = (
                item["object_asset_id"]
                if direction == "→"
                else item["subject_asset_id"]
            )
            lines.append(f"- **{item['relation_type']}** {direction} [[{other}]]")
    else:
        lines.append("- 无")
    lines.append("")

    lines.extend(["## 我的批注", ""])
    if annotations:
        for annotation in annotations:
            lines.extend(
                [
                    f"### {annotation['title'] or annotation['kind']}",
                    "",
                    annotation["body_markdown"] or "无",
                    "",
                ]
            )
    else:
        lines.extend(["无", ""])

    lines.extend(
        [
            "## 托管元数据",
            "",
            "> [!info] 以下 ID、来源位置、摘要值与哈希由 Research Asset Hub 管理。",
            "",
            f"- Asset ID：`{asset['asset_id']}`",
            f"- 来源记录：{len(document['provenance']['source_records'])}",
            f"- 字段声明：{len(document['provenance']['field_claims'])}",
            "",
        ]
    )
    return "\n".join(lines)


def _collect_documents(session: Session) -> list[dict[str, Any]]:
    assets = list(
        session.scalars(
            select(Asset)
            .where(Asset.deleted_at.is_(None))
            .order_by(Asset.asset_type, Asset.title, Asset.id)
        )
    )
    sources = {source.id: source for source in session.scalars(select(Source))}
    records = {record.id: record for record in session.scalars(select(SourceRecord))}
    versions = {
        version.id: version for version in session.scalars(select(SourceRecordVersion))
    }
    links_by_asset: dict[Any, list[SourceRecordAsset]] = defaultdict(list)
    for source_link in session.scalars(
        select(SourceRecordAsset).where(SourceRecordAsset.deleted_at.is_(None))
    ):
        links_by_asset[source_link.asset_id].append(source_link)
    identifiers_by_asset: dict[Any, list[ExternalIdentifierModel]] = defaultdict(list)
    for identifier in session.scalars(select(ExternalIdentifierModel)):
        identifiers_by_asset[identifier.asset_id].append(identifier)
    representations_by_asset: dict[Any, list[Representation]] = defaultdict(list)
    for representation in session.scalars(
        select(Representation).where(Representation.deleted_at.is_(None))
    ):
        representations_by_asset[representation.asset_id].append(representation)
    relations_by_asset: dict[Any, list[Relation]] = defaultdict(list)
    relation_ids: set[Any] = set()
    for relation in session.scalars(select(Relation).where(Relation.deleted_at.is_(None))):
        relations_by_asset[relation.subject_asset_id].append(relation)
        relations_by_asset[relation.object_asset_id].append(relation)
        relation_ids.add(relation.id)
    evidence_by_relation: dict[Any, list[RelationEvidenceModel]] = defaultdict(list)
    if relation_ids:
        for relation_evidence in session.scalars(
            select(RelationEvidenceModel).where(
                RelationEvidenceModel.relation_id.in_(relation_ids)
            )
        ):
            evidence_by_relation[relation_evidence.relation_id].append(relation_evidence)
    claims_by_asset: dict[Any, list[FieldClaim]] = defaultdict(list)
    for field_claim in session.scalars(
        select(FieldClaim).where(FieldClaim.entity_type == "asset")
    ):
        claims_by_asset[field_claim.entity_id].append(field_claim)
    resolutions_by_asset: dict[Any, list[FieldResolution]] = defaultdict(list)
    for field_resolution in session.scalars(
        select(FieldResolution).where(FieldResolution.entity_type == "asset")
    ):
        resolutions_by_asset[field_resolution.entity_id].append(field_resolution)
    state_by_asset = {
        reading_state.asset_id: reading_state
        for reading_state in session.scalars(
            select(ReadingState).where(ReadingState.user_id == "local")
        )
    }
    annotations_by_asset: dict[Any, list[Annotation]] = defaultdict(list)
    for annotation in session.scalars(
        select(Annotation).where(
            Annotation.user_id == "local", Annotation.deleted_at.is_(None)
        )
    ):
        annotations_by_asset[annotation.asset_id].append(annotation)

    documents: list[dict[str, Any]] = []
    for asset in assets:
        representation_payload: list[dict[str, Any]] = []
        for representation in sorted(
            representations_by_asset[asset.id],
            key=lambda value: (value.representation_type, value.path_ref or ""),
        ):
            source = (
                sources.get(representation.source_id)
                if representation.source_id is not None
                else None
            )
            representation_payload.append(
                {
                    "representation_id": str(representation.id),
                    "representation_type": representation.representation_type,
                    "role": representation.role,
                    "materialization_kind": representation.materialization_kind,
                    "availability": representation.availability,
                    "source_alias": source.alias if source else None,
                    "root_key": representation.root_key,
                    "path_ref": representation.path_ref,
                    "external_url": representation.external_url,
                    "entrypoint": representation.entrypoint,
                    "revision": representation.revision,
                    "tree_digest": representation.tree_digest,
                    "sha256": representation.sha256,
                    "mime_type": representation.mime_type,
                    "size_bytes": representation.size_bytes,
                    "network_required": representation.network_required,
                    "attributes": representation.attributes,
                }
            )

        relation_payload: list[dict[str, Any]] = []
        for relation in sorted(
            relations_by_asset[asset.id],
            key=lambda value: (value.relation_type, str(value.id)),
        ):
            relation_payload.append(
                {
                    "relation_id": str(relation.id),
                    "subject_asset_id": str(relation.subject_asset_id),
                    "relation_type": relation.relation_type,
                    "object_asset_id": str(relation.object_asset_id),
                    "confirmation_status": relation.confirmation_status,
                    "confidence": relation.confidence,
                    "evidence": [
                        {
                            "source_key": evidence.source_key,
                            "provider": evidence.provider,
                            "evidence": evidence.evidence,
                            "confidence": evidence.confidence,
                            "evidence_hash": evidence.evidence_hash,
                        }
                        for evidence in evidence_by_relation[relation.id]
                    ],
                }
            )

        provenance_records: list[dict[str, Any]] = []
        for source_link in links_by_asset[asset.id]:
            record = records[source_link.source_record_id]
            source = sources[record.source_id]
            version = (
                versions.get(source_link.source_record_version_id)
                if source_link.source_record_version_id is not None
                else None
            )
            provenance_records.append(
                {
                    "source_alias": source.alias,
                    "source_record_id": record.source_record_id,
                    "source_record_version_id": str(version.id) if version else None,
                    "raw_digest": version.raw_digest if version else None,
                    "role": source_link.role,
                    "outcome": record.outcome,
                    "warnings": record.warnings,
                }
            )

        reading_state = state_by_asset.get(asset.id)
        document = {
            "export_schema": EXPORT_SCHEMA,
            "asset": {
                **_asset_payload(asset),
                "external_identifiers": [
                    {
                        "scheme": identifier.scheme,
                        "scope": identifier.scope,
                        "value": identifier.value,
                        "canonical": identifier.canonical,
                        "is_identity": identifier.is_identity,
                    }
                    for identifier in identifiers_by_asset[asset.id]
                ],
            },
            "representations": representation_payload,
            "relations": relation_payload,
            "reading_state": (
                {
                    "status": reading_state.status,
                    "started_at": (
                        reading_state.started_at.isoformat()
                        if reading_state.started_at
                        else None
                    ),
                    "read_at": (
                        reading_state.read_at.isoformat() if reading_state.read_at else None
                    ),
                }
                if reading_state
                else None
            ),
            "annotations": [
                {
                    "annotation_id": str(annotation.id),
                    "kind": annotation.kind,
                    "title": annotation.title,
                    "summary": annotation.summary,
                    "body_markdown": annotation.body_markdown,
                    "anchor": annotation.anchor,
                    "anchor_status": annotation.anchor_status,
                    "row_version": annotation.row_version,
                }
                for annotation in annotations_by_asset[asset.id]
            ],
            "provenance": {
                "source_records": provenance_records,
                "field_claims": [
                    {
                        "field_path": claim.field_path,
                        "value": claim.value_json,
                        "value_digest": claim.value_digest,
                        "source_key": claim.source_key,
                        "source_pointer": claim.source_pointer,
                        "authority": claim.authority,
                        "status": claim.status,
                    }
                    for claim in claims_by_asset[asset.id]
                ],
                "field_resolutions": [
                    {
                        "field_path": resolution.field_path,
                        "resolution_kind": resolution.resolution_kind,
                        "selected_claim_id": (
                            str(resolution.selected_claim_id)
                            if resolution.selected_claim_id
                            else None
                        ),
                        "override_value": resolution.override_value,
                        "reason": resolution.reason,
                    }
                    for resolution in resolutions_by_asset[asset.id]
                ],
            },
        }
        documents.append(document)
    return documents


def _write_asset_directory(path: Path, document: dict[str, Any]) -> list[Path]:
    return [
        atomic_write_json(path / "asset.json", document["asset"]),
        atomic_write_json(path / "representations.json", document["representations"]),
        atomic_write_json(path / "relations.json", document["relations"]),
        atomic_write_json(path / "provenance.json", document["provenance"]),
        atomic_write_text(path / "note.md", _render_note(document)),
    ]


def export_operational_catalog(session: Session, destination: Path) -> dict[str, Any]:
    """Write a stable open catalog and return its manifest."""

    generated_at = datetime.now(UTC)
    documents = _collect_documents(session)
    written: list[Path] = []
    lines: list[str] = []
    crate_parts: list[dict[str, str]] = []
    by_domain: dict[str, int] = defaultdict(int)

    for document in documents:
        asset = document["asset"]
        asset_id = asset["asset_id"]
        canonical = destination / "assets" / asset_id
        written.extend(_write_asset_directory(canonical, document))
        domains = asset["domains"] or ["unclassified"]
        for domain in domains:
            by_domain[str(domain)] += 1
            domain_path = destination / "by-domain" / str(domain) / asset_id
            written.extend(_write_asset_directory(domain_path, document))
        lines.append(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        crate_parts.append(
            {
                "@id": f"assets/{asset_id}/asset.json",
                "@type": "CreativeWork",
                "name": asset["title"],
            }
        )

    catalog_path = atomic_write_text(
        destination / "catalog.jsonl",
        "\n".join(lines) + ("\n" if lines else ""),
    )
    written.append(catalog_path)
    crate_path = atomic_write_json(
        destination / "ro-crate-metadata.json",
        {
            "@context": "https://w3id.org/ro/crate/1.1/context",
            "@graph": [
                {
                    "@id": "ro-crate-metadata.json",
                    "@type": "CreativeWork",
                    "about": {"@id": "./"},
                },
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Research Asset Hub open catalog",
                    "datePublished": generated_at.isoformat(),
                    "hasPart": [{"@id": item["@id"]} for item in crate_parts],
                },
                *crate_parts,
            ],
        },
    )
    written.append(crate_path)
    manifest = {
        "schema": EXPORT_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "asset_count": len(documents),
        "by_domain": dict(sorted(by_domain.items())),
        "file_count": len(written) + 1,
        "source_binaries_copied": 0,
        "catalog": "catalog.jsonl",
        "ro_crate": "ro-crate-metadata.json",
    }
    atomic_write_json(destination / "manifest.json", manifest)
    return manifest
