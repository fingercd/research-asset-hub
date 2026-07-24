"""Add processing, search, RAG, integrations and backup state.

Revision ID: 0003_search_rag_integrations
Revises: 0002_platform_core
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_search_rag_integrations"
down_revision: str | None = "0002_platform_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID_TYPE = sa.Uuid()
NOW = sa.text("CURRENT_TIMESTAMP")


def _timestamps_and_version() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("job_type", sa.String(length=127), nullable=False),
        sa.Column("parameters", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("progress", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=512), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_job_status",
        ),
        sa.CheckConstraint(
            "progress >= 0.0 AND progress <= 1.0",
            name="ck_jobs_job_progress",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1",
            name="ck_jobs_job_attempt_counts",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint("job_type", "dedupe_key", name="uq_job_dedupe"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "priority", "scheduled_at"])
    op.create_index("ix_jobs_heartbeat_at", "jobs", ["heartbeat_at"])
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_lease_until", "jobs", ["lease_until"])
    op.create_index("ix_jobs_scheduled_at", "jobs", ["scheduled_at"])

    op.create_table(
        "job_attempts",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("job_id", UUID_TYPE, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'abandoned', 'cancelled')",
            name="ck_job_attempts_job_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_job_attempts_job_attempt_number_positive",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_job_attempts_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_attempts"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])

    op.create_table(
        "activities",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("job_id", UUID_TYPE, nullable=True),
        sa.Column("asset_id", UUID_TYPE, nullable=True),
        sa.Column("representation_id", UUID_TYPE, nullable=True),
        sa.Column("activity_type", sa.String(length=127), nullable=False),
        sa.Column("tool_name", sa.String(length=127), nullable=False),
        sa.Column("tool_version", sa.String(length=127), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("output_digest", sa.String(length=64), nullable=True),
        sa.Column("parameters", JSON_DOCUMENT, nullable=False),
        sa.Column("result", JSON_DOCUMENT, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped')",
            name="ck_activities_activity_status",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_activities_asset_id_assets",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_activities_job_id_jobs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["representation_id"],
            ["representations.id"],
            name="fk_activities_representation_id_representations",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_activities"),
    )
    op.create_index("ix_activities_activity_type", "activities", ["activity_type"])
    op.create_index("ix_activities_asset_id", "activities", ["asset_id"])
    op.create_index("ix_activities_job_id", "activities", ["job_id"])
    op.create_index(
        "ix_activities_representation_id",
        "activities",
        ["representation_id"],
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("representation_id", UUID_TYPE, nullable=False),
        sa.Column("activity_id", UUID_TYPE, nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_kind", sa.String(length=64), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_name", sa.String(length=127), nullable=False),
        sa.Column("parser_version", sa.String(length=127), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=True),
        sa.Column("anchor_kind", sa.String(length=32), nullable=True),
        sa.Column("anchor", JSON_DOCUMENT, nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "anchor_kind IS NULL OR anchor_kind IN "
            "('pdf_page', 'html_heading', 'text_lines', 'code_lines', 'notebook_cell')",
            name="ck_document_chunks_document_chunk_anchor_kind",
        ),
        sa.CheckConstraint(
            "chunk_index >= 0",
            name="ck_document_chunks_document_chunk_index",
        ),
        sa.CheckConstraint(
            "token_count IS NULL OR token_count >= 0",
            name="ck_document_chunks_document_chunk_token_count",
        ),
        sa.ForeignKeyConstraint(
            ["activity_id"],
            ["activities.id"],
            name="fk_document_chunks_activity_id_activities",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_document_chunks_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["representation_id"],
            ["representations.id"],
            name="fk_document_chunks_representation_id_representations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint(
            "representation_id",
            "parser_name",
            "parser_version",
            "chunk_index",
            name="uq_document_chunk_parser_index",
        ),
    )
    op.create_index("ix_document_chunks_activity_id", "document_chunks", ["activity_id"])
    op.create_index("ix_document_chunks_asset_id", "document_chunks", ["asset_id"])
    op.create_index(
        "ix_document_chunks_asset_kind",
        "document_chunks",
        ["asset_id", "chunk_kind"],
    )
    op.create_index("ix_document_chunks_content_hash", "document_chunks", ["content_hash"])
    op.create_index(
        "ix_document_chunks_representation_id",
        "document_chunks",
        ["representation_id"],
    )

    op.create_table(
        "index_states",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("index_name", sa.String(length=127), nullable=False),
        sa.Column("index_kind", sa.String(length=16), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", UUID_TYPE, nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("provider", sa.String(length=127), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("dimension", sa.Integer(), nullable=True),
        sa.Column("version", sa.String(length=127), nullable=False),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "index_kind IN ('keyword', 'vector', 'code')",
            name="ck_index_states_index_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'indexing', 'ready', 'stale', 'failed')",
            name="ck_index_states_index_status",
        ),
        sa.CheckConstraint(
            "dimension IS NULL OR dimension > 0",
            name="ck_index_states_index_dimension",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_index_states"),
        sa.UniqueConstraint(
            "index_name",
            "entity_type",
            "entity_id",
            "content_digest",
            "version",
            name="uq_index_state_entity_version",
        ),
    )
    op.create_index("ix_index_states_index_name", "index_states", ["index_name"])
    op.create_index("ix_index_states_status", "index_states", ["status"])

    op.create_table(
        "outbox_events",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", UUID_TYPE, nullable=False),
        sa.Column("event_type", sa.String(length=127), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        sa.Column("dedupe_key", sa.String(length=512), nullable=False),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'failed')",
            name="ck_outbox_events_outbox_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_event_dedupe"),
    )
    op.create_index(
        "ix_outbox_events_claim",
        "outbox_events",
        ["status", "available_at"],
    )

    op.create_table(
        "provider_usage",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("provider", sa.String(length=127), nullable=False),
        sa.Column("usage_kind", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("job_id", UUID_TYPE, nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("document_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("actual_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("privacy_scope", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("details", JSON_DOCUMENT, nullable=False),
        sa.CheckConstraint(
            "usage_kind IN ('embedding', 'chat', 'rerank', 'ocr')",
            name="ck_provider_usage_usage_kind",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND document_count >= 0",
            name="ck_provider_usage_provider_usage_counts",
        ),
        sa.CheckConstraint(
            "(estimated_cost IS NULL OR estimated_cost >= 0) AND "
            "(actual_cost IS NULL OR actual_cost >= 0)",
            name="ck_provider_usage_provider_usage_costs",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_provider_usage_job_id_jobs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_usage"),
        sa.UniqueConstraint("request_id", name="uq_provider_usage_request_id"),
    )
    op.create_index("ix_provider_usage_job_id", "provider_usage", ["job_id"])
    op.create_index("ix_provider_usage_model", "provider_usage", ["model"])
    op.create_index("ix_provider_usage_occurred_at", "provider_usage", ["occurred_at"])
    op.create_index("ix_provider_usage_provider", "provider_usage", ["provider"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("user_id", sa.String(length=127), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("filters", JSON_DOCUMENT, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
    )
    op.create_index("ix_chat_sessions_deleted_at", "chat_sessions", ["deleted_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("session_id", UUID_TYPE, nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="complete", nullable=False),
        sa.Column("provider", sa.String(length=127), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=127), nullable=True),
        sa.Column("retrieval_snapshot", JSON_DOCUMENT, nullable=False),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'complete', 'failed', 'cancelled')",
            name="ck_chat_messages_chat_message_status",
        ),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_chat_messages_chat_message_role",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_chat_messages_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
        sa.UniqueConstraint("session_id", "sequence", name="uq_chat_message_sequence"),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    op.create_table(
        "citations",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("message_id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("representation_id", UUID_TYPE, nullable=False),
        sa.Column("chunk_id", UUID_TYPE, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("anchor", JSON_DOCUMENT, nullable=True),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("relevance", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "relevance IS NULL OR (relevance >= 0.0 AND relevance <= 1.0)",
            name="ck_citations_citation_relevance",
        ),
        sa.CheckConstraint(
            "ordinal >= 1",
            name="ck_citations_citation_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_citations_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name="fk_citations_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_messages.id"],
            name="fk_citations_message_id_chat_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["representation_id"],
            ["representations.id"],
            name="fk_citations_representation_id_representations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_citations"),
        sa.UniqueConstraint("message_id", "ordinal", name="uq_citation_message_ordinal"),
    )
    op.create_index("ix_citations_asset_id", "citations", ["asset_id"])
    op.create_index("ix_citations_chunk_id", "citations", ["chunk_id"])
    op.create_index("ix_citations_message_id", "citations", ["message_id"])
    op.create_index(
        "ix_citations_representation_id",
        "citations",
        ["representation_id"],
    )

    op.create_table(
        "saved_views",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("user_id", sa.String(length=127), nullable=False),
        sa.Column("view_kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("definition", JSON_DOCUMENT, nullable=False),
        sa.Column("visibility", sa.String(length=16), server_default="private", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "view_kind IN ('search', 'comparison', 'graph', 'domain')",
            name="ck_saved_views_saved_view_kind",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'internal', 'public')",
            name="ck_saved_views_saved_view_visibility",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_views"),
        sa.UniqueConstraint(
            "user_id",
            "view_kind",
            "name",
            name="uq_saved_view_user_name",
        ),
    )
    op.create_index("ix_saved_views_deleted_at", "saved_views", ["deleted_at"])

    op.create_table(
        "integrations",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("provider", sa.String(length=127), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("config", JSON_DOCUMENT, nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="ready", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('disabled', 'ready', 'syncing', 'error')",
            name="ck_integrations_integration_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integrations"),
        sa.UniqueConstraint("provider", "name", name="uq_integration_provider_name"),
    )
    op.create_index("ix_integrations_provider", "integrations", ["provider"])

    op.create_table(
        "integration_objects",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("integration_id", UUID_TYPE, nullable=False),
        sa.Column("library_scope", sa.String(length=255), nullable=False),
        sa.Column("external_type", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=True),
        sa.Column("annotation_id", UUID_TYPE, nullable=True),
        sa.Column("external_version", sa.String(length=255), nullable=True),
        sa.Column("external_hash", sa.String(length=64), nullable=True),
        sa.Column("hub_version", sa.Integer(), nullable=True),
        sa.Column("external_path", sa.Text(), nullable=True),
        sa.Column("remote_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "asset_id IS NOT NULL OR annotation_id IS NOT NULL",
            name="ck_integration_objects_integration_object_target",
        ),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["annotations.id"],
            name="fk_integration_objects_annotation_id_annotations",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_integration_objects_asset_id_assets",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name="fk_integration_objects_integration_id_integrations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_objects"),
        sa.UniqueConstraint(
            "integration_id",
            "library_scope",
            "external_type",
            "external_id",
            name="uq_integration_external_object",
        ),
    )
    op.create_index(
        "ix_integration_objects_annotation_id",
        "integration_objects",
        ["annotation_id"],
    )
    op.create_index(
        "ix_integration_objects_asset_id",
        "integration_objects",
        ["asset_id"],
    )
    op.create_index(
        "ix_integration_objects_integration_id",
        "integration_objects",
        ["integration_id"],
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("integration_id", UUID_TYPE, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("base_cursor", sa.Text(), nullable=True),
        sa.Column("remote_cursor", sa.Text(), nullable=True),
        sa.Column("preview_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("counters", JSON_DOCUMENT, nullable=False),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN "
            "('pending', 'running', 'succeeded', 'failed', 'cancelled', 'needs_review')",
            name="ck_sync_runs_sync_status",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name="fk_sync_runs_integration_id_integrations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
    )
    op.create_index("ix_sync_runs_integration_id", "sync_runs", ["integration_id"])

    op.create_table(
        "sync_snapshots",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("external_object_id", UUID_TYPE, nullable=False),
        sa.Column("sync_run_id", UUID_TYPE, nullable=True),
        sa.Column("base_document", JSON_DOCUMENT, nullable=False),
        sa.Column("hub_revision", sa.Integer(), nullable=True),
        sa.Column("external_revision", sa.String(length=255), nullable=True),
        sa.Column("hub_hash", sa.String(length=64), nullable=True),
        sa.Column("external_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["external_object_id"],
            ["integration_objects.id"],
            name="fk_sync_snapshots_external_object_id_integration_objects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            name="fk_sync_snapshots_sync_run_id_sync_runs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_snapshots"),
        sa.UniqueConstraint(
            "external_object_id",
            "sync_run_id",
            name="uq_sync_snapshot_object_run",
        ),
    )
    op.create_index(
        "ix_sync_snapshots_external_object_id",
        "sync_snapshots",
        ["external_object_id"],
    )
    op.create_index("ix_sync_snapshots_sync_run_id", "sync_snapshots", ["sync_run_id"])

    op.create_table(
        "sync_conflicts",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("sync_run_id", UUID_TYPE, nullable=False),
        sa.Column("external_object_id", UUID_TYPE, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", UUID_TYPE, nullable=False),
        sa.Column("field_path", sa.String(length=512), nullable=False),
        sa.Column("base_value", JSON_DOCUMENT, nullable=True),
        sa.Column("hub_value", JSON_DOCUMENT, nullable=True),
        sa.Column("external_value", JSON_DOCUMENT, nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.Column("resolved_value", JSON_DOCUMENT, nullable=True),
        sa.Column("resolved_by", sa.String(length=127), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'ignored')",
            name="ck_sync_conflicts_sync_conflict_status",
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('hub', 'external', 'base', 'manual', 'delete')",
            name="ck_sync_conflicts_sync_conflict_resolution",
        ),
        sa.ForeignKeyConstraint(
            ["external_object_id"],
            ["integration_objects.id"],
            name="fk_sync_conflicts_external_object_id_integration_objects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            name="fk_sync_conflicts_sync_run_id_sync_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_conflicts"),
        sa.UniqueConstraint(
            "sync_run_id",
            "external_object_id",
            "field_path",
            name="uq_sync_conflict_run_field",
        ),
    )
    op.create_index(
        "ix_sync_conflicts_external_object_id",
        "sync_conflicts",
        ["external_object_id"],
    )
    op.create_index("ix_sync_conflicts_status", "sync_conflicts", ["status"])
    op.create_index("ix_sync_conflicts_sync_run_id", "sync_conflicts", ["sync_run_id"])

    op.create_table(
        "tombstones",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("integration_id", UUID_TYPE, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", UUID_TYPE, nullable=True),
        sa.Column("external_type", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('active', 'restored', 'acknowledged')",
            name="ck_tombstones_tombstone_status",
        ),
        sa.CheckConstraint(
            "side IN ('hub', 'external')",
            name="ck_tombstones_tombstone_side",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name="fk_tombstones_integration_id_integrations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tombstones"),
        sa.UniqueConstraint(
            "integration_id",
            "side",
            "external_type",
            "external_id",
            name="uq_tombstone_external_object",
        ),
    )
    op.create_index("ix_tombstones_integration_id", "tombstones", ["integration_id"])

    op.create_table(
        "backup_runs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("backup_kind", sa.String(length=32), nullable=False),
        sa.Column("destination_label", sa.String(length=255), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=True),
        sa.Column("database_digest", sa.String(length=64), nullable=True),
        sa.Column("export_digest", sa.String(length=64), nullable=True),
        sa.Column("local_copy_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("counters", JSON_DOCUMENT, nullable=False),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'verified')",
            name="ck_backup_runs_backup_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backup_runs"),
    )
    op.create_index("ix_backup_runs_status", "backup_runs", ["status"])


def downgrade() -> None:
    op.drop_table("backup_runs")
    op.drop_table("tombstones")
    op.drop_table("sync_conflicts")
    op.drop_table("sync_snapshots")
    op.drop_table("sync_runs")
    op.drop_table("integration_objects")
    op.drop_table("integrations")
    op.drop_table("saved_views")
    op.drop_table("citations")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_table("provider_usage")
    op.drop_table("outbox_events")
    op.drop_table("index_states")
    op.drop_table("document_chunks")
    op.drop_table("activities")
    op.drop_table("job_attempts")
    op.drop_table("jobs")
