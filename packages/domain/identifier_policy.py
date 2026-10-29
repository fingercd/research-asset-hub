"""Audited identity decisions scoped to an asset and its frozen source records."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from packages.domain.models import ExternalIdentifierModel, FieldResolution, Source, SourceRecord

IDENTITY_FIELD_PATH = "/identity"
RejectedIdentityKeys = frozenset[tuple[str, str, str, str, str]]


def effective_identifier_clause() -> ColumnElement[bool]:
    """Only accepted identity declarations enter matching and search aliases."""
    return ExternalIdentifierModel.is_identity.is_(True)


def identity_resolution(session: Session, identifier_id: UUID) -> FieldResolution | None:
    return session.scalar(
        select(FieldResolution).where(
            FieldResolution.entity_type == "external_identifier",
            FieldResolution.entity_id == identifier_id,
            FieldResolution.field_path == IDENTITY_FIELD_PATH,
            FieldResolution.resolution_kind == "override",
        )
    )


def source_identity_scope(session: Session, asset_id: UUID) -> list[dict[str, str]]:
    """Capture existing stable keys; never reject an identifier for an entire source."""
    return [
        {"source_id": alias, "source_record_id": key, "record_id": str(record_id)}
        for alias, key, record_id in session.execute(
            select(Source.alias, SourceRecord.source_record_id, SourceRecord.id)
            .join(SourceRecord, SourceRecord.source_id == Source.id)
            .where(SourceRecord.asset_id == asset_id)
            .order_by(Source.alias, SourceRecord.source_record_id)
        )
    ]


def rejected_identity_keys(session: Session) -> RejectedIdentityKeys:
    """Read decisions from the current transaction; failures must reach the caller."""
    keys = set()
    for control in session.scalars(
        select(FieldResolution).where(
            FieldResolution.entity_type == "external_identifier",
            FieldResolution.field_path == IDENTITY_FIELD_PATH,
            FieldResolution.resolution_kind == "override",
        )
    ):
        value = cast(dict[str, Any], control.override_value)
        if not value["rejected"]:
            continue
        for record in value["source_records"]:
            keys.add(
                (
                    record["source_id"],
                    record["source_record_id"],
                    value["scheme"],
                    value["scope"],
                    value["canonical"],
                )
            )
    return frozenset(keys)
