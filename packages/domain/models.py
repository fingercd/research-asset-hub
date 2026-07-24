"""SQLAlchemy 2.x operational models.

The model deliberately stores extensible asset and domain names as strings,
while state machines and relation types are protected by database checks.
Source files remain read-only: deletion in this database is a soft deletion or
an unlink operation and never implies deletion of a source representation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

from .enums import (
    AnnotationKind,
    AuditAction,
    ConfirmationStatus,
    ImportOutcome,
    ImportStatus,
    ReadingStatus,
    RelationType,
    RepresentationRole,
    RepresentationType,
    Visibility,
)
from .identifiers import new_uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def _enum_check(values: type[Any]) -> str:
    return "(" + ", ".join(f"'{item.value}'" for item in values) + ")"


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampVersionMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
        nullable=False,
    )
    row_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
        return {"version_id_col": cls.row_version}


class Source(TimestampVersionMixin, Base):
    __tablename__ = "sources"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    alias: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter: Mapped[str] = mapped_column(String(127), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    read_only: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

    records: Mapped[list[SourceRecord]] = relationship(back_populates="source")
    import_runs: Mapped[list[ImportRun]] = relationship(back_populates="source")


class ImportRun(TimestampVersionMixin, Base):
    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(ImportStatus)}",
            name="import_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    source_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"),
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=ImportStatus.PENDING.value,
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(64), default="incremental", nullable=False)
    dry_run: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    adapter_name: Mapped[str | None] = mapped_column(String(127))
    adapter_version: Mapped[str | None] = mapped_column(String(64))
    config_digest: Mapped[str | None] = mapped_column(String(64))
    counters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[Source | None] = relationship(back_populates="import_runs")
    source_records: Mapped[list[SourceRecord]] = relationship(back_populates="import_run")


class Asset(TimestampVersionMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "year IS NULL OR (year >= 1450 AND year <= 2200)",
            name="asset_year_range",
        ),
        CheckConstraint(
            f"visibility IN {_enum_check(Visibility)}",
            name="asset_visibility",
        ),
        Index("ix_assets_type_year", "asset_type", "year"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    asset_type: Mapped[str] = mapped_column(String(65), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_zh: Mapped[str | None] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    venue: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(35))
    abstract: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(16),
        default=Visibility.PRIVATE.value,
        nullable=False,
    )
    source_urls: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    domain_slugs: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    source_records: Mapped[list[SourceRecord]] = relationship(back_populates="asset")
    external_identifiers: Mapped[list[ExternalIdentifierModel]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    representations: Mapped[list[Representation]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    outgoing_relations: Mapped[list[Relation]] = relationship(
        foreign_keys="Relation.subject_asset_id",
        back_populates="subject_asset",
    )
    incoming_relations: Mapped[list[Relation]] = relationship(
        foreign_keys="Relation.object_asset_id",
        back_populates="object_asset",
    )
    reading_states: Mapped[list[ReadingState]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    annotations: Mapped[list[Annotation]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )


class ExternalIdentifierModel(Base):
    __tablename__ = "external_identifiers"
    __table_args__ = (
        UniqueConstraint("scheme", "canonical", name="uq_external_identifier_authority"),
        UniqueConstraint(
            "asset_id",
            "scheme",
            "canonical",
            name="uq_external_identifier_per_asset",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    canonical: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )

    asset: Mapped[Asset] = relationship(back_populates="external_identifiers")


class SourceRecord(TimestampVersionMixin, Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "source_record_id",
            name="uq_source_record_identity",
        ),
        CheckConstraint(
            f"outcome IN {_enum_check(ImportOutcome)}",
            name="source_record_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_record_id: Mapped[str] = mapped_column(String(512), nullable=False)
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"),
        index=True,
    )
    import_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("import_runs.id", ondelete="SET NULL"),
        index=True,
    )
    outcome: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    source_fields: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )
    normalized_digest: Mapped[str | None] = mapped_column(String(64))
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT,
        default=list,
        nullable=False,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )

    source: Mapped[Source] = relationship(back_populates="records")
    asset: Mapped[Asset | None] = relationship(back_populates="source_records")
    import_run: Mapped[ImportRun | None] = relationship(back_populates="source_records")


class Representation(TimestampVersionMixin, Base):
    __tablename__ = "representations"
    __table_args__ = (
        CheckConstraint(
            f"representation_type IN {_enum_check(RepresentationType)}",
            name="representation_type",
        ),
        CheckConstraint(
            f"role IN {_enum_check(RepresentationRole)}",
            name="representation_role",
        ),
        CheckConstraint(
            "path_ref IS NOT NULL OR external_url IS NOT NULL",
            name="representation_location",
        ),
        UniqueConstraint(
            "source_id",
            "root_key",
            "path_ref",
            "role",
            name="uq_representation_source_path_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"),
        index=True,
    )
    representation_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    root_key: Mapped[str] = mapped_column(String(63), default="source", nullable=False)
    path_ref: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    network_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    asset: Mapped[Asset] = relationship(back_populates="representations")
    source: Mapped[Source | None] = relationship()
    annotations: Mapped[list[Annotation]] = relationship(back_populates="representation")


class Relation(TimestampVersionMixin, Base):
    __tablename__ = "relations"
    __table_args__ = (
        CheckConstraint(
            f"relation_type IN {_enum_check(RelationType)}",
            name="relation_type",
        ),
        CheckConstraint(
            f"confirmation_status IN {_enum_check(ConfirmationStatus)}",
            name="relation_confirmation_status",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="relation_confidence",
        ),
        CheckConstraint(
            "subject_asset_id <> object_asset_id",
            name="relation_non_self",
        ),
        UniqueConstraint(
            "subject_asset_id",
            "relation_type",
            "object_asset_id",
            "source_key",
            name="uq_relation_evidenced_edge",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    subject_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    object_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(255), default="manual", nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    confirmation_status: Mapped[str] = mapped_column(
        String(16),
        default=ConfirmationStatus.CANDIDATE.value,
        nullable=False,
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    subject_asset: Mapped[Asset] = relationship(
        foreign_keys=[subject_asset_id],
        back_populates="outgoing_relations",
    )
    object_asset: Mapped[Asset] = relationship(
        foreign_keys=[object_asset_id],
        back_populates="incoming_relations",
    )


class ReadingState(TimestampVersionMixin, Base):
    __tablename__ = "reading_states"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(ReadingStatus)}",
            name="reading_status",
        ),
        UniqueConstraint("user_id", "asset_id", name="uq_reading_state_user_asset"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=ReadingStatus.UNREAD.value,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    asset: Mapped[Asset] = relationship(back_populates="reading_states")


class Annotation(TimestampVersionMixin, Base):
    __tablename__ = "annotations"
    __table_args__ = (
        CheckConstraint(
            f"kind IN {_enum_check(AnnotationKind)}",
            name="annotation_kind",
        ),
        Index("ix_annotations_asset_pinned", "asset_id", "pinned"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    representation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("representations.id", ondelete="SET NULL"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        default=AnnotationKind.COMMENT.value,
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(String(512))
    body_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    anchor: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    pinned: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    asset: Mapped[Asset] = relationship(back_populates="annotations")
    representation: Mapped[Representation | None] = relationship(back_populates="annotations")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            f"action IN {_enum_check(AuditAction)}",
            name="audit_action",
        ),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(127), index=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
        index=True,
    )
