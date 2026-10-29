"""Post-0001 platform models for provenance, processing and integrations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .enums import (
    ActivityStatus,
    AnchorKind,
    AttemptStatus,
    BackupStatus,
    ChatRole,
    ConflictResolution,
    FieldClaimStatus,
    FieldResolutionKind,
    IdentityCandidateStatus,
    IndexKind,
    IndexStatus,
    IntegrationStatus,
    JobStatus,
    MessageStatus,
    OutboxStatus,
    ProfileStatus,
    SavedViewKind,
    SourceAssetRole,
    SyncConflictStatus,
    SyncStatus,
    TombstoneStatus,
    UsageKind,
    Visibility,
)
from .identifiers import new_uuid7
from .models import (
    JSON_DOCUMENT,
    Base,
    TimestampVersionMixin,
    _enum_check,
    utc_now,
)


class PaperProfile(TimestampVersionMixin, Base):
    __tablename__ = "paper_profiles"
    __table_args__ = (
        CheckConstraint(
            f"publication_status IN {_enum_check(ProfileStatus)}",
            name="paper_publication_status",
        ),
    )

    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    publication_status: Mapped[str] = mapped_column(
        String(16),
        default=ProfileStatus.UNKNOWN.value,
        server_default=ProfileStatus.UNKNOWN.value,
        nullable=False,
    )
    paper_type: Mapped[str | None] = mapped_column(String(127))
    research_question: Mapped[str | None] = mapped_column(Text)
    mechanism: Mapped[str | None] = mapped_column(Text)
    contributions: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class RepositoryProfile(TimestampVersionMixin, Base):
    __tablename__ = "repository_profiles"

    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    canonical_url: Mapped[str | None] = mapped_column(Text, unique=True)
    host: Mapped[str | None] = mapped_column(String(255), index=True)
    owner: Mapped[str | None] = mapped_column(String(255), index=True)
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    pinned_revision: Mapped[str | None] = mapped_column(String(255), index=True)
    license_spdx: Mapped[str | None] = mapped_column(String(127))
    code_status: Mapped[str] = mapped_column(
        String(32),
        default="unknown",
        server_default="unknown",
        nullable=False,
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class SourceRecordVersion(Base):
    __tablename__ = "source_record_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_record_id",
            "raw_digest",
            name="uq_source_record_version_digest",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    import_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("import_runs.id", ondelete="SET NULL"),
        index=True,
    )
    adapter_version: Mapped[str | None] = mapped_column(String(64))
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    source_fields: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )
    raw_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class SourceRecordAsset(TimestampVersionMixin, Base):
    __tablename__ = "source_record_assets"
    __table_args__ = (
        CheckConstraint(
            f"role IN {_enum_check(SourceAssetRole)}",
            name="source_asset_role",
        ),
        UniqueConstraint(
            "source_record_id",
            "asset_id",
            "role",
            name="uq_source_record_asset_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_record_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "source_record_versions.id",
            name="fk_sr_assets_version",
            ondelete="SET NULL",
        ),
        index=True,
    )
    role: Mapped[str] = mapped_column(
        String(32),
        default=SourceAssetRole.PRIMARY.value,
        server_default=SourceAssetRole.PRIMARY.value,
        nullable=False,
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class FieldClaim(TimestampVersionMixin, Base):
    __tablename__ = "field_claims"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(FieldClaimStatus)}",
            name="field_claim_status",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="field_claim_confidence",
        ),
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            "value_digest",
            "source_key",
            name="uq_field_claim_evidence",
        ),
        Index("ix_field_claim_entity_field", "entity_type", "entity_id", "field_path"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON_DOCUMENT, nullable=True)
    value_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_record_versions.id", ondelete="SET NULL"),
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(127), index=True)
    source_pointer: Mapped[str | None] = mapped_column(String(1024))
    source_key: Mapped[str] = mapped_column(String(255), default="manual", nullable=False)
    authority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(16),
        default=FieldClaimStatus.CANDIDATE.value,
        server_default=FieldClaimStatus.CANDIDATE.value,
        nullable=False,
        index=True,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class FieldResolution(TimestampVersionMixin, Base):
    __tablename__ = "field_resolutions"
    __table_args__ = (
        CheckConstraint(
            f"resolution_kind IN {_enum_check(FieldResolutionKind)}",
            name="field_resolution_kind",
        ),
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            name="uq_field_resolution_entity_path",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    resolution_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    selected_claim_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("field_claims.id", ondelete="SET NULL"),
        index=True,
    )
    override_value: Mapped[Any | None] = mapped_column(JSON_DOCUMENT)
    reason: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str] = mapped_column(String(127), default="local", nullable=False)


class IdentityCandidate(TimestampVersionMixin, Base):
    __tablename__ = "identity_candidates"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(IdentityCandidateStatus)}",
            name="identity_candidate_status",
        ),
        CheckConstraint(
            "left_asset_id <> right_asset_id",
            name="identity_candidate_non_self",
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0.0 AND score <= 1.0)",
            name="identity_candidate_score",
        ),
        UniqueConstraint(
            "left_asset_id",
            "right_asset_id",
            "reason",
            name="uq_identity_candidate_pair_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    left_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    right_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(16),
        default=IdentityCandidateStatus.PENDING.value,
        server_default=IdentityCandidateStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(127))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetRedirect(Base):
    __tablename__ = "asset_redirects"
    __table_args__ = (
        CheckConstraint("from_asset_id <> to_asset_id", name="asset_redirect_non_self"),
    )

    from_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    to_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    merge_audit_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("audit_events.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class RepresentationMount(TimestampVersionMixin, Base):
    __tablename__ = "representation_mounts"
    __table_args__ = (
        UniqueConstraint(
            "representation_id",
            "mount_path",
            name="uq_representation_mount_path",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    representation_id: Mapped[UUID] = mapped_column(
        ForeignKey("representations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mount_path: Mapped[str] = mapped_column(String(1024), default="/", nullable=False)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    root_key: Mapped[str] = mapped_column(String(63), default="source", nullable=False)
    path_ref: Mapped[str] = mapped_column(Text, nullable=False)
    read_only: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RelationEvidenceModel(Base):
    __tablename__ = "relation_evidence"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="relation_evidence_confidence",
        ),
        UniqueConstraint(
            "relation_id",
            "evidence_hash",
            name="uq_relation_evidence_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    relation_id: Mapped[UUID] = mapped_column(
        ForeignKey("relations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_record_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "source_record_versions.id",
            name="fk_relation_evidence_sr_version",
            ondelete="SET NULL",
        ),
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(127), index=True)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class AnnotationRevision(Base):
    __tablename__ = "annotation_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="annotation_revision_positive"),
        UniqueConstraint(
            "annotation_id",
            "revision",
            name="uq_annotation_revision_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    annotation_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(String(512))
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    editor: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    source: Mapped[str] = mapped_column(String(127), default="hub", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class Job(TimestampVersionMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"status IN {_enum_check(JobStatus)}", name="job_status"),
        CheckConstraint("progress >= 0.0 AND progress <= 1.0", name="job_progress"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="job_attempt_counts"),
        UniqueConstraint("job_type", "dedupe_key", name="uq_job_dedupe"),
        Index("ix_jobs_claim", "status", "priority", "scheduled_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    job_type: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=JobStatus.PENDING.value,
        server_default=JobStatus.PENDING.value,
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    progress: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        server_default=text("0"),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=3,
        server_default=text("3"),
        nullable=False,
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(512))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(AttemptStatus)}",
            name="job_attempt_status",
        ),
        CheckConstraint("attempt_number >= 1", name="job_attempt_number_positive"),
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=AttemptStatus.RUNNING.value,
        server_default=AttemptStatus.RUNNING.value,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(ActivityStatus)}",
            name="activity_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
        index=True,
    )
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"),
        index=True,
    )
    representation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("representations.id", ondelete="SET NULL"),
        index=True,
    )
    activity_type: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(127), nullable=False)
    tool_version: Mapped[str | None] = mapped_column(String(127))
    status: Mapped[str] = mapped_column(
        String(16),
        default=ActivityStatus.RUNNING.value,
        server_default=ActivityStatus.RUNNING.value,
        nullable=False,
    )
    input_digest: Mapped[str | None] = mapped_column(String(64))
    output_digest: Mapped[str | None] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint(
            f"anchor_kind IS NULL OR anchor_kind IN {_enum_check(AnchorKind)}",
            name="document_chunk_anchor_kind",
        ),
        CheckConstraint("chunk_index >= 0", name="document_chunk_index"),
        CheckConstraint(
            "token_count IS NULL OR token_count >= 0",
            name="document_chunk_token_count",
        ),
        UniqueConstraint(
            "representation_id",
            "parser_name",
            "parser_version",
            "chunk_index",
            name="uq_document_chunk_parser_index",
        ),
        Index("ix_document_chunks_asset_kind", "asset_id", "chunk_kind"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    representation_id: Mapped[UUID] = mapped_column(
        ForeignKey("representations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    activity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"),
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parser_name: Mapped[str] = mapped_column(String(127), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(127), nullable=False)
    language: Mapped[str | None] = mapped_column(String(35))
    anchor_kind: Mapped[str | None] = mapped_column(String(32))
    anchor: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    token_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class IndexState(TimestampVersionMixin, Base):
    __tablename__ = "index_states"
    __table_args__ = (
        CheckConstraint(f"index_kind IN {_enum_check(IndexKind)}", name="index_kind"),
        CheckConstraint(f"status IN {_enum_check(IndexStatus)}", name="index_status"),
        CheckConstraint(
            "dimension IS NULL OR dimension > 0",
            name="index_dimension",
        ),
        UniqueConstraint(
            "index_name",
            "entity_type",
            "entity_id",
            "content_digest",
            "version",
            name="uq_index_state_entity_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    index_name: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    index_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=IndexStatus.PENDING.value,
        server_default=IndexStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(127))
    model: Mapped[str | None] = mapped_column(String(255))
    dimension: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(127), default="1", nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(TimestampVersionMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(f"status IN {_enum_check(OutboxStatus)}", name="outbox_status"),
        UniqueConstraint("dedupe_key", name="uq_outbox_event_dedupe"),
        Index("ix_outbox_events_claim", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(127), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=OutboxStatus.PENDING.value,
        server_default=OutboxStatus.PENDING.value,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    __table_args__ = (
        CheckConstraint(f"usage_kind IN {_enum_check(UsageKind)}", name="usage_kind"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND document_count >= 0",
            name="provider_usage_counts",
        ),
        CheckConstraint(
            "(estimated_cost IS NULL OR estimated_cost >= 0) AND "
            "(actual_cost IS NULL OR actual_cost >= 0)",
            name="provider_usage_costs",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    provider: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    usage_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(255), index=True)
    request_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
        index=True,
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    document_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    privacy_scope: Mapped[str] = mapped_column(String(32), default="private", nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class ChatSession(TimestampVersionMixin, Base):
    __tablename__ = "chat_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ChatMessage(TimestampVersionMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(MessageStatus)}",
            name="chat_message_status",
        ),
        CheckConstraint(f"role IN {_enum_check(ChatRole)}", name="chat_message_role"),
        UniqueConstraint("session_id", "sequence", name="uq_chat_message_sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=MessageStatus.COMPLETE.value,
        server_default=MessageStatus.COMPLETE.value,
        nullable=False,
    )
    provider: Mapped[str | None] = mapped_column(String(127))
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(127))
    retrieval_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (
        CheckConstraint(
            "relevance IS NULL OR (relevance >= 0.0 AND relevance <= 1.0)",
            name="citation_relevance",
        ),
        CheckConstraint("ordinal >= 1", name="citation_ordinal"),
        UniqueConstraint("message_id", "ordinal", name="uq_citation_message_ordinal"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    representation_id: Mapped[UUID] = mapped_column(
        ForeignKey("representations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    anchor: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    relevance: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class SavedView(TimestampVersionMixin, Base):
    __tablename__ = "saved_views"
    __table_args__ = (
        CheckConstraint(
            f"view_kind IN {_enum_check(SavedViewKind)}",
            name="saved_view_kind",
        ),
        CheckConstraint(
            f"visibility IN {_enum_check(Visibility)}",
            name="saved_view_visibility",
        ),
        UniqueConstraint("user_id", "view_kind", "name", name="uq_saved_view_user_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    view_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(16),
        default=Visibility.PRIVATE.value,
        server_default=Visibility.PRIVATE.value,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Integration(TimestampVersionMixin, Base):
    __tablename__ = "integrations"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(IntegrationStatus)}",
            name="integration_status",
        ),
        UniqueConstraint("provider", "name", name="uq_integration_provider_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    provider: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=IntegrationStatus.READY.value,
        server_default=IntegrationStatus.READY.value,
        nullable=False,
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)


class IntegrationObject(TimestampVersionMixin, Base):
    __tablename__ = "integration_objects"
    __table_args__ = (
        CheckConstraint(
            "asset_id IS NOT NULL OR annotation_id IS NOT NULL",
            name="integration_object_target",
        ),
        UniqueConstraint(
            "integration_id",
            "library_scope",
            "external_type",
            "external_id",
            name="uq_integration_external_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    integration_id: Mapped[UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    library_scope: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    external_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"),
        index=True,
    )
    annotation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("annotations.id", ondelete="SET NULL"),
        index=True,
    )
    external_version: Mapped[str | None] = mapped_column(String(255))
    external_hash: Mapped[str | None] = mapped_column(String(64))
    hub_version: Mapped[int | None] = mapped_column(Integer)
    external_path: Mapped[str | None] = mapped_column(Text)
    remote_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncRun(TimestampVersionMixin, Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(f"status IN {_enum_check(SyncStatus)}", name="sync_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    integration_id: Mapped[UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=SyncStatus.PENDING.value,
        server_default=SyncStatus.PENDING.value,
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(32), default="incremental", nullable=False)
    base_cursor: Mapped[str | None] = mapped_column(Text)
    remote_cursor: Mapped[str | None] = mapped_column(Text)
    preview_only: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    counters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncSnapshot(Base):
    __tablename__ = "sync_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "external_object_id",
            "sync_run_id",
            name="uq_sync_snapshot_object_run",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    external_object_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sync_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"),
        index=True,
    )
    base_document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    hub_revision: Mapped[int | None] = mapped_column(Integer)
    external_revision: Mapped[str | None] = mapped_column(String(255))
    hub_hash: Mapped[str | None] = mapped_column(String(64))
    external_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class SyncConflict(TimestampVersionMixin, Base):
    __tablename__ = "sync_conflicts"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(SyncConflictStatus)}",
            name="sync_conflict_status",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN "
            f"{_enum_check(ConflictResolution)}",
            name="sync_conflict_resolution",
        ),
        UniqueConstraint(
            "sync_run_id",
            "external_object_id",
            "field_path",
            name="uq_sync_conflict_run_field",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    sync_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_object_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_objects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    base_value: Mapped[Any | None] = mapped_column(JSON_DOCUMENT)
    hub_value: Mapped[Any | None] = mapped_column(JSON_DOCUMENT)
    external_value: Mapped[Any | None] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(
        String(16),
        default=SyncConflictStatus.PENDING.value,
        server_default=SyncConflictStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    resolution: Mapped[str | None] = mapped_column(String(16))
    resolved_value: Mapped[Any | None] = mapped_column(JSON_DOCUMENT)
    resolved_by: Mapped[str | None] = mapped_column(String(127))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Tombstone(TimestampVersionMixin, Base):
    __tablename__ = "tombstones"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(TombstoneStatus)}",
            name="tombstone_status",
        ),
        CheckConstraint("side IN ('hub', 'external')", name="tombstone_side"),
        UniqueConstraint(
            "integration_id",
            "side",
            "external_type",
            "external_id",
            name="uq_tombstone_external_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    integration_id: Mapped[UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid)
    external_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=TombstoneStatus.ACTIVE.value,
        server_default=TombstoneStatus.ACTIVE.value,
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BackupRun(TimestampVersionMixin, Base):
    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_enum_check(BackupStatus)}",
            name="backup_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    status: Mapped[str] = mapped_column(
        String(16),
        default=BackupStatus.PENDING.value,
        server_default=BackupStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    backup_kind: Mapped[str] = mapped_column(String(32), default="full", nullable=False)
    destination_label: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    database_digest: Mapped[str | None] = mapped_column(String(64))
    export_digest: Mapped[str | None] = mapped_column(String(64))
    local_copy_only: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    counters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
