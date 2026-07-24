"""Shared domain contract for the Research Asset Hub.

Adapters, API handlers and workers should import public types from this
module (or from :mod:`packages.domain.schemas`) instead of defining local
variants of the ingestion contract.
"""

from .enums import (
    AnnotationKind,
    AssetType,
    AuditAction,
    ConfirmationStatus,
    ImportOutcome,
    ImportStatus,
    ReadingStatus,
    RelationType,
    RepresentationRole,
    RepresentationType,
    Visibility,
    WarningSeverity,
)
from .identifiers import UUID7, new_uuid7
from .schemas import (
    AdapterWarning,
    AssetReference,
    ExternalIdentifier,
    NormalizedAsset,
    NormalizedRecord,
    RelationCandidate,
    RelationEvidence,
    RepresentationCandidate,
    SourceConfig,
)

__all__ = [
    "AdapterWarning",
    "AnnotationKind",
    "AssetReference",
    "AssetType",
    "AuditAction",
    "ConfirmationStatus",
    "ExternalIdentifier",
    "ImportOutcome",
    "ImportStatus",
    "NormalizedAsset",
    "NormalizedRecord",
    "ReadingStatus",
    "RelationCandidate",
    "RelationEvidence",
    "RelationType",
    "RepresentationCandidate",
    "RepresentationRole",
    "RepresentationType",
    "SourceConfig",
    "UUID7",
    "Visibility",
    "WarningSeverity",
    "new_uuid7",
]
