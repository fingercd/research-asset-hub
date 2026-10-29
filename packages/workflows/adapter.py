"""Read the immutable package snapshot in ordinary source validation/imports."""

from __future__ import annotations

from packages.adapters.base import BaseAdapter
from packages.adapters.types import ScannedRecord
from packages.domain.enums import MaterializationKind, RepresentationAvailability
from packages.domain.schemas import (
    AssetReference,
    ExternalIdentifier,
    NormalizedAsset,
    NormalizedRecord,
    RelationCandidate,
    RelationEvidence,
    RepresentationCandidate,
)


class DomainPackageAdapter(BaseAdapter):
    adapter_name = "domain_package"
    adapter_version = "1"

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        raw = record.raw
        metadata = {
            key: value for key, value in raw["asset"].items() if key not in {"id", "identifiers"}
        }
        metadata["asset_id"] = raw["target_asset_id"]
        metadata["external_identifiers"] = [
            ExternalIdentifier.model_validate(item) for item in raw["asset"].get("identifiers", [])
        ]
        representations = []
        for item in raw["representations"]:
            if item.get("reuse_representation_id"):
                continue
            if not item.get("path_ref") and not item.get("external_url"):
                continue
            representations.append(
                RepresentationCandidate(
                    representation_type=item["representation_type"],
                    role=item["role"],
                    materialization_kind=MaterializationKind(item["materialization_kind"])
                    if item.get("path_ref")
                    else MaterializationKind.URL,
                    availability=RepresentationAvailability.AVAILABLE,
                    root_key="attachments",
                    path_ref=item.get("path_ref"),
                    url=item.get("external_url"),
                    entrypoint=item.get("entrypoint"),
                    revision=item.get("revision"),
                    sha256=item.get("sha256"),
                    tree_digest=item.get("tree_digest"),
                    mime_type=item.get("mime_type"),
                    size_bytes=sum(file["size_bytes"] for file in item.get("files", [])) or None,
                    network_required=not bool(item.get("path_ref")),
                    metadata={
                        "archive_representation_id": item["id"],
                        **(
                            {"package_content_tree_digest": item["package_content_tree_digest"]}
                            if item.get("package_content_tree_digest")
                            else {}
                        ),
                    },
                )
            )
        relations = [
            RelationCandidate(
                relation_type=item["relation_type"],
                object=AssetReference(
                    source_id=self.config.source_id, source_record_id=item["object_asset_id"]
                ),
                confirmed=item["confirmation_status"] == "confirmed",
                confidence=item.get("confidence"),
                evidence=RelationEvidence(
                    source=self.config.source_id, details={"archive_relation_id": item["id"]}
                ),
            )
            for item in raw.get("relations", [])
            if item["confirmation_status"] != "rejected" and not item.get("reuse_relation_id")
        ]
        return NormalizedRecord(
            source_id=self.config.source_id,
            source_record_id=raw["id"],
            source_path=record.source_path,
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            asset=NormalizedAsset.model_validate(metadata),
            representations=representations,
            relations=relations,
            source_fields=raw["asset"],
            raw=raw,
        )
