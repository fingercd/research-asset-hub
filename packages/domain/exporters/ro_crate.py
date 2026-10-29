"""Minimal RO-Crate 1.1 JSON-LD export.

The crate does not copy source files.  It describes immutable source
representations through stable ``source://`` identifiers so a future packager
can materialize only the files the user explicitly chooses.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..schemas import AssetReference, NormalizedRecord
from .common import atomic_write_json, jsonable

RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"


def _reference_id(reference: AssetReference, default_source: str) -> str:
    if reference.asset_id is not None:
        return f"urn:uuid:{reference.asset_id}"
    if reference.external_identifier is not None:
        identifier = reference.external_identifier
        encoded = quote(identifier.canonical or identifier.value, safe="/")
        return f"urn:{identifier.scheme}:{encoded}"
    return (
        f"source-record://{reference.source_id or default_source}/"
        f"{quote(reference.source_record_id or '', safe='')}"
    )


def _representation_id(record: NormalizedRecord, index: int) -> str:
    representation = record.representations[index]
    if representation.path_ref:
        return (
            f"source://{representation.source_id or record.source_id}/"
            f"{representation.root_key}/{quote(representation.path_ref, safe='/')}"
        )
    return str(representation.url)


def export_ro_crate(
    records: Iterable[NormalizedRecord],
    destination: str | Path,
    *,
    reading_states: Iterable[dict[str, Any]] = (),
    annotations: Iterable[dict[str, Any]] = (),
    name: str = "Research Asset Hub catalog",
) -> Path:
    """Write an RO-Crate metadata document and return its path."""

    destination_path = Path(destination)
    metadata_path = (
        destination_path
        if destination_path.name == "ro-crate-metadata.json"
        else destination_path / "ro-crate-metadata.json"
    )
    graph: list[dict[str, Any]] = [
        {
            "@id": "ro-crate-metadata.json",
            "@type": "CreativeWork",
            "about": {"@id": "./"},
            "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
        },
        {
            "@id": "./",
            "@type": "Dataset",
            "name": name,
            "hasPart": [],
        },
    ]
    root = graph[1]

    for record in records:
        asset = record.asset
        asset_uri = f"urn:uuid:{asset.asset_id}"
        node: dict[str, Any] = {
            "@id": asset_uri,
            "@type": "ScholarlyArticle" if asset.asset_type == "paper" else "CreativeWork",
            "name": asset.title,
            "identifier": [
                f"{identifier.scheme}:{identifier.canonical}"
                for identifier in asset.external_identifiers
            ],
            "keywords": [*asset.domain_slugs, *asset.tags],
            "isPartOf": {"@id": "./"},
            "hasPart": [],
        }
        if asset.authors:
            node["author"] = [{"@type": "Person", "name": author} for author in asset.authors]
        if asset.year is not None:
            node["datePublished"] = str(asset.year)
        if asset.abstract or asset.summary:
            node["abstract"] = asset.abstract or asset.summary
        root["hasPart"].append({"@id": asset_uri})
        graph.append(node)

        for index, representation in enumerate(record.representations):
            representation_uri = _representation_id(record, index)
            node["hasPart"].append({"@id": representation_uri})
            graph.append(
                {
                    "@id": representation_uri,
                    "@type": "Dataset"
                    if representation.representation_type.value in {"dataset", "data"}
                    else "File",
                    "encodingFormat": representation.mime_type,
                    "contentSize": representation.size_bytes,
                    "sha256": representation.sha256,
                    "additionalType": representation.representation_type.value,
                    "isPartOf": {"@id": asset_uri},
                    "sourceRoot": representation.root_key,
                }
            )

        for relation in record.relations:
            graph.append(
                {
                    "@id": (
                        f"{asset_uri}#relation-"
                        f"{len([item for item in graph if item.get('@type') == 'Relationship'])}"
                    ),
                    "@type": "Relationship",
                    "subject": {"@id": asset_uri}
                    if relation.subject is None
                    else {"@id": _reference_id(relation.subject, record.source_id)},
                    "object": {"@id": _reference_id(relation.object, record.source_id)},
                    "predicate": relation.relation_type.value,
                    "confidence": relation.confidence,
                    "evidence": jsonable(relation.evidence),
                }
            )

    for index, state in enumerate(reading_states):
        graph.append(
            {
                "@id": f"#reading-state-{index}",
                "@type": "Action",
                "object": {"@id": f"urn:uuid:{state.get('asset_id')}"},
                "actionStatus": state.get("status"),
                "description": "Personal reading state",
            }
        )
    for index, annotation in enumerate(annotations):
        graph.append(
            {
                "@id": f"#annotation-{annotation.get('id', index)}",
                "@type": "Comment",
                "about": {"@id": f"urn:uuid:{annotation.get('asset_id')}"},
                "name": annotation.get("title") or annotation.get("kind") or "Annotation",
                "text": annotation.get("body_markdown") or annotation.get("body") or "",
                "position": jsonable(annotation.get("anchor")),
            }
        )

    document = {"@context": RO_CRATE_CONTEXT, "@graph": graph}
    return atomic_write_json(metadata_path, document)
