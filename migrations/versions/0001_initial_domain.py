"""Create the Research Asset Hub operational domain tables.

Revision ID: 0001_initial_domain
Revises:
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_domain"
down_revision: str | None = None
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
        "sources",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("alias", sa.String(length=63), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("adapter", sa.String(length=127), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=True),
        sa.Column("read_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("config", JSON_DOCUMENT, nullable=False),
        *_timestamps_and_version(),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("alias", name="uq_sources_alias"),
    )

    op.create_table(
        "import_runs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_id", UUID_TYPE, nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=64), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("adapter_name", sa.String(length=127), nullable=True),
        sa.Column("adapter_version", sa.String(length=64), nullable=True),
        sa.Column("config_digest", sa.String(length=64), nullable=True),
        sa.Column("counters", JSON_DOCUMENT, nullable=False),
        sa.Column("error", JSON_DOCUMENT, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_import_runs_import_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_import_runs_source_id_sources",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_runs"),
    )
    op.create_index("ix_import_runs_source_id", "import_runs", ["source_id"])

    op.create_table(
        "assets",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("asset_type", sa.String(length=65), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_zh", sa.Text(), nullable=True),
        sa.Column("subtitle", sa.Text(), nullable=True),
        sa.Column("authors", JSON_DOCUMENT, nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("venue", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=35), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("analysis", JSON_DOCUMENT, nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("source_urls", JSON_DOCUMENT, nullable=False),
        sa.Column("domain_slugs", JSON_DOCUMENT, nullable=False),
        sa.Column("tags", JSON_DOCUMENT, nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "year IS NULL OR (year >= 1450 AND year <= 2200)",
            name="ck_assets_asset_year_range",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'internal', 'public')",
            name="ck_assets_asset_visibility",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
    )
    op.create_index("ix_assets_asset_type", "assets", ["asset_type"])
    op.create_index("ix_assets_deleted_at", "assets", ["deleted_at"])
    op.create_index("ix_assets_type_year", "assets", ["asset_type", "year"])
    op.create_index("ix_assets_year", "assets", ["year"])

    op.create_table(
        "external_identifiers",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("canonical", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_external_identifiers_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_identifiers"),
        sa.UniqueConstraint(
            "scheme",
            "canonical",
            name="uq_external_identifier_authority",
        ),
        sa.UniqueConstraint(
            "asset_id",
            "scheme",
            "canonical",
            name="uq_external_identifier_per_asset",
        ),
    )
    op.create_index(
        "ix_external_identifiers_asset_id",
        "external_identifiers",
        ["asset_id"],
    )

    op.create_table(
        "source_records",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_id", UUID_TYPE, nullable=False),
        sa.Column("source_record_id", sa.String(length=512), nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=True),
        sa.Column("import_run_id", UUID_TYPE, nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("raw_data", JSON_DOCUMENT, nullable=False),
        sa.Column("source_fields", JSON_DOCUMENT, nullable=False),
        sa.Column("normalized_digest", sa.String(length=64), nullable=True),
        sa.Column("warnings", JSON_DOCUMENT, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "outcome IN ('ready', 'skipped', 'error')",
            name="ck_source_records_source_record_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_source_records_asset_id_assets",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name="fk_source_records_import_run_id_import_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_source_records_source_id_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_records"),
        sa.UniqueConstraint(
            "source_id",
            "source_record_id",
            name="uq_source_record_identity",
        ),
    )
    op.create_index("ix_source_records_asset_id", "source_records", ["asset_id"])
    op.create_index("ix_source_records_import_run_id", "source_records", ["import_run_id"])
    op.create_index("ix_source_records_source_id", "source_records", ["source_id"])

    op.create_table(
        "representations",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("source_id", UUID_TYPE, nullable=True),
        sa.Column("representation_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("root_key", sa.String(length=63), nullable=False),
        sa.Column("path_ref", sa.Text(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("network_required", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "representation_type IN "
            "('pdf', 'html', 'repository', 'source_code', 'annotation', 'guide', "
            "'analysis', 'markdown', 'notebook', 'dataset', 'data', 'image', "
            "'extracted_text', 'thumbnail', 'external_url')",
            name="ck_representations_representation_type",
        ),
        sa.CheckConstraint(
            "role IN ('primary', 'original', 'official', 'mirror', 'analysis', "
            "'guide', 'annotated', 'supplemental', 'snapshot', 'derived')",
            name="ck_representations_representation_role",
        ),
        sa.CheckConstraint(
            "path_ref IS NOT NULL OR external_url IS NOT NULL",
            name="ck_representations_representation_location",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_representations_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_representations_source_id_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_representations"),
        sa.UniqueConstraint(
            "source_id",
            "root_key",
            "path_ref",
            "role",
            name="uq_representation_source_path_role",
        ),
    )
    op.create_index("ix_representations_asset_id", "representations", ["asset_id"])
    op.create_index("ix_representations_deleted_at", "representations", ["deleted_at"])
    op.create_index(
        "ix_representations_representation_type",
        "representations",
        ["representation_type"],
    )
    op.create_index("ix_representations_sha256", "representations", ["sha256"])
    op.create_index("ix_representations_source_id", "representations", ["source_id"])

    op.create_table(
        "relations",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("subject_asset_id", UUID_TYPE, nullable=False),
        sa.Column("object_asset_id", UUID_TYPE, nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confirmation_status", sa.String(length=16), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "relation_type IN ('cites', 'cited_by', 'extends', 'extended_by', "
            "'precedes', 'follows', 'related_to', 'implements', 'implemented_by', "
            "'has_representation', 'uses_method', 'method_used_by', 'uses_dataset', "
            "'dataset_used_by', 'same_as', 'is_version_of', 'has_version', "
            "'derived_from', 'generated', 'belongs_to_collection', 'similar_to')",
            name="ck_relations_relation_type",
        ),
        sa.CheckConstraint(
            "confirmation_status IN ('candidate', 'confirmed', 'rejected')",
            name="ck_relations_relation_confirmation_status",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_relations_relation_confidence",
        ),
        sa.CheckConstraint(
            "subject_asset_id <> object_asset_id",
            name="ck_relations_relation_non_self",
        ),
        sa.ForeignKeyConstraint(
            ["object_asset_id"],
            ["assets.id"],
            name="fk_relations_object_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_asset_id"],
            ["assets.id"],
            name="fk_relations_subject_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_relations"),
        sa.UniqueConstraint(
            "subject_asset_id",
            "relation_type",
            "object_asset_id",
            "source_key",
            name="uq_relation_evidenced_edge",
        ),
    )
    op.create_index("ix_relations_deleted_at", "relations", ["deleted_at"])
    op.create_index("ix_relations_object_asset_id", "relations", ["object_asset_id"])
    op.create_index("ix_relations_relation_type", "relations", ["relation_type"])
    op.create_index("ix_relations_subject_asset_id", "relations", ["subject_asset_id"])

    op.create_table(
        "reading_states",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("user_id", sa.String(length=127), nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('unread', 'reading', 'read')",
            name="ck_reading_states_reading_status",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_reading_states_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reading_states"),
        sa.UniqueConstraint(
            "user_id",
            "asset_id",
            name="uq_reading_state_user_asset",
        ),
    )
    op.create_index("ix_reading_states_asset_id", "reading_states", ["asset_id"])

    op.create_table(
        "annotations",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("user_id", sa.String(length=127), nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("representation_id", UUID_TYPE, nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("anchor", JSON_DOCUMENT, nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "kind IN ('general', 'comment', 'summary', 'question', 'highlight')",
            name="ck_annotations_annotation_kind",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_annotations_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["representation_id"],
            ["representations.id"],
            name="fk_annotations_representation_id_representations",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotations"),
    )
    op.create_index("ix_annotations_asset_id", "annotations", ["asset_id"])
    op.create_index("ix_annotations_asset_pinned", "annotations", ["asset_id", "pinned"])
    op.create_index("ix_annotations_deleted_at", "annotations", ["deleted_at"])
    op.create_index(
        "ix_annotations_representation_id",
        "annotations",
        ["representation_id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", UUID_TYPE, nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=127), nullable=False),
        sa.Column("request_id", sa.String(length=127), nullable=True),
        sa.Column("before", JSON_DOCUMENT, nullable=True),
        sa.Column("after", JSON_DOCUMENT, nullable=True),
        sa.Column("details", JSON_DOCUMENT, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "action IN ('create', 'update', 'delete', 'restore', 'import', 'merge')",
            name="ck_audit_events_audit_action",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("annotations")
    op.drop_table("reading_states")
    op.drop_table("relations")
    op.drop_table("representations")
    op.drop_table("source_records")
    op.drop_table("external_identifiers")
    op.drop_table("assets")
    op.drop_table("import_runs")
    op.drop_table("sources")
