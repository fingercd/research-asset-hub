"""Add repository materialization, provenance and versioned annotation core.

Revision ID: 0002_platform_core
Revises: 0001_initial_domain
Create Date: 2026-07-24
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_platform_core"
down_revision: str | None = "0001_initial_domain"
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


def _backfill_source_versions_and_assets() -> None:
    connection = op.get_bind()
    source_records = sa.table(
        "source_records",
        sa.column("id", UUID_TYPE),
        sa.column("asset_id", UUID_TYPE),
        sa.column("import_run_id", UUID_TYPE),
        sa.column("raw_data", JSON_DOCUMENT),
        sa.column("source_fields", JSON_DOCUMENT),
        sa.column("normalized_digest", sa.String(64)),
        sa.column("last_seen_at", sa.DateTime(timezone=True)),
    )
    version_table = sa.table(
        "source_record_versions",
        sa.column("id", UUID_TYPE),
        sa.column("source_record_id", UUID_TYPE),
        sa.column("import_run_id", UUID_TYPE),
        sa.column("adapter_version", sa.String(64)),
        sa.column("raw_data", JSON_DOCUMENT),
        sa.column("source_fields", JSON_DOCUMENT),
        sa.column("raw_digest", sa.String(64)),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    mapping_table = sa.table(
        "source_record_assets",
        sa.column("id", UUID_TYPE),
        sa.column("source_record_id", UUID_TYPE),
        sa.column("asset_id", UUID_TYPE),
        sa.column("source_record_version_id", UUID_TYPE),
        sa.column("role", sa.String(32)),
        sa.column("evidence", JSON_DOCUMENT),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("row_version", sa.Integer),
    )
    rows = connection.execute(sa.select(source_records)).mappings().all()
    now = datetime.now(UTC)
    for row in rows:
        raw_data = row["raw_data"] or {}
        canonical = json.dumps(
            raw_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        raw_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        connection.execute(
            version_table.insert().values(
                id=row["id"],
                source_record_id=row["id"],
                import_run_id=row["import_run_id"],
                adapter_version=None,
                raw_data=raw_data,
                source_fields=row["source_fields"] or {},
                raw_digest=raw_digest,
                observed_at=row["last_seen_at"] or now,
                created_at=now,
            )
        )
        if row["asset_id"] is not None:
            connection.execute(
                mapping_table.insert().values(
                    id=row["id"],
                    source_record_id=row["id"],
                    asset_id=row["asset_id"],
                    source_record_version_id=row["id"],
                    role="primary",
                    evidence={"migration": revision, "legacy_field": "asset_id"},
                    deleted_at=None,
                    created_at=now,
                    updated_at=now,
                    row_version=1,
                )
            )


def upgrade() -> None:
    with op.batch_alter_table("external_identifiers") as batch:
        batch.add_column(
            sa.Column("scope", sa.String(length=255), server_default=sa.text("''"), nullable=False)
        )
        batch.add_column(
            sa.Column("is_identity", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch.drop_constraint("uq_external_identifier_authority", type_="unique")
        batch.drop_constraint("uq_external_identifier_per_asset", type_="unique")
        batch.create_unique_constraint(
            "uq_external_identifier_per_asset",
            ["asset_id", "scheme", "scope", "canonical"],
        )
    op.execute(
        sa.text(
            "UPDATE external_identifiers SET is_identity = "
            "CASE WHEN scheme IN ('doi', 'arxiv', 'github', 'zotero', 'isbn', 'source') "
            "THEN true ELSE false END"
        )
    )
    op.create_index(
        "uq_external_identifier_identity",
        "external_identifiers",
        ["scheme", "scope", "canonical"],
        unique=True,
        postgresql_where=sa.text("is_identity"),
        sqlite_where=sa.text("is_identity = 1"),
    )

    with op.batch_alter_table("representations") as batch:
        batch.drop_constraint(
            "uq_representation_source_path_role",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_representation_asset_source_path_role",
            ["asset_id", "source_id", "root_key", "path_ref", "role"],
        )
        batch.add_column(sa.Column("source_record_id", UUID_TYPE, nullable=True))
        batch.add_column(
            sa.Column(
                "materialization_kind",
                sa.String(length=16),
                server_default="file",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "availability",
                sa.String(length=16),
                server_default="unknown",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("entrypoint", sa.Text(), nullable=True))
        batch.add_column(sa.Column("revision", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("tree_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_representations_source_record_id_source_records",
            "source_records",
            ["source_record_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_representations_representation_materialization_kind",
            "materialization_kind IN ('file', 'directory', 'url', 'bundle')",
        )
        batch.create_check_constraint(
            "ck_representations_representation_availability",
            "availability IN ('unknown', 'available', 'missing', 'stale', 'blocked')",
        )
    op.create_index(
        "ix_representations_source_record_id",
        "representations",
        ["source_record_id"],
    )
    op.create_index("ix_representations_availability", "representations", ["availability"])
    op.create_index("ix_representations_revision", "representations", ["revision"])
    op.create_index("ix_representations_tree_digest", "representations", ["tree_digest"])
    op.execute(
        sa.text(
            "UPDATE representations SET materialization_kind = 'directory' "
            "WHERE representation_type IN ('repository', 'source_code')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE representations SET source_record_id = "
            "(SELECT sr.id FROM source_records sr "
            "WHERE sr.asset_id = representations.asset_id "
            "AND sr.source_id = representations.source_id LIMIT 1)"
        )
    )

    with op.batch_alter_table("annotations") as batch:
        batch.add_column(sa.Column("anchor_kind", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("anchor_digest", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "anchor_status",
                sa.String(length=16),
                server_default="current",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_annotations_annotation_anchor_kind",
            "anchor_kind IS NULL OR anchor_kind IN "
            "('pdf_page', 'html_heading', 'text_lines', 'code_lines', 'notebook_cell')",
        )
        batch.create_check_constraint(
            "ck_annotations_annotation_anchor_status",
            "anchor_status IN ('current', 'stale', 'needs_review', 'orphaned')",
        )

    op.create_table(
        "paper_profiles",
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column(
            "publication_status",
            sa.String(length=16),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("paper_type", sa.String(length=127), nullable=True),
        sa.Column("research_question", sa.Text(), nullable=True),
        sa.Column("mechanism", sa.Text(), nullable=True),
        sa.Column("contributions", JSON_DOCUMENT, nullable=False),
        sa.Column("evidence_summary", sa.Text(), nullable=True),
        sa.Column("limitations", JSON_DOCUMENT, nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "publication_status IN ('unknown', 'draft', 'published', 'withdrawn')",
            name="ck_paper_profiles_paper_publication_status",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_paper_profiles_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", name="pk_paper_profiles"),
    )

    op.create_table(
        "repository_profiles",
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("owner", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("default_branch", sa.String(length=255), nullable=True),
        sa.Column("pinned_revision", sa.String(length=255), nullable=True),
        sa.Column("license_spdx", sa.String(length=127), nullable=True),
        sa.Column("code_status", sa.String(length=32), server_default="unknown", nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        *_timestamps_and_version(),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_repository_profiles_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", name="pk_repository_profiles"),
        sa.UniqueConstraint(
            "canonical_url",
            name="uq_repository_profiles_canonical_url",
        ),
    )
    op.create_index("ix_repository_profiles_host", "repository_profiles", ["host"])
    op.create_index("ix_repository_profiles_name", "repository_profiles", ["name"])
    op.create_index("ix_repository_profiles_owner", "repository_profiles", ["owner"])
    op.create_index(
        "ix_repository_profiles_pinned_revision",
        "repository_profiles",
        ["pinned_revision"],
    )

    op.create_table(
        "source_record_versions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_record_id", UUID_TYPE, nullable=False),
        sa.Column("import_run_id", UUID_TYPE, nullable=True),
        sa.Column("adapter_version", sa.String(length=64), nullable=True),
        sa.Column("raw_data", JSON_DOCUMENT, nullable=False),
        sa.Column("source_fields", JSON_DOCUMENT, nullable=False),
        sa.Column("raw_digest", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name="fk_source_record_versions_import_run_id_import_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["source_records.id"],
            name="fk_source_record_versions_source_record_id_source_records",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_record_versions"),
        sa.UniqueConstraint(
            "source_record_id",
            "raw_digest",
            name="uq_source_record_version_digest",
        ),
    )
    op.create_index(
        "ix_source_record_versions_import_run_id",
        "source_record_versions",
        ["import_run_id"],
    )
    op.create_index(
        "ix_source_record_versions_source_record_id",
        "source_record_versions",
        ["source_record_id"],
    )

    op.create_table(
        "source_record_assets",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_record_id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("source_record_version_id", UUID_TYPE, nullable=True),
        sa.Column("role", sa.String(length=32), server_default="primary", nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "role IN ('primary', 'repository', 'dataset', 'supplemental', 'mentioned')",
            name="ck_source_record_assets_source_asset_role",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_source_record_assets_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["source_records.id"],
            name="fk_source_record_assets_source_record_id_source_records",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_version_id"],
            ["source_record_versions.id"],
            name="fk_sr_assets_version",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_record_assets"),
        sa.UniqueConstraint(
            "source_record_id",
            "asset_id",
            "role",
            name="uq_source_record_asset_role",
        ),
    )
    op.create_index("ix_source_record_assets_asset_id", "source_record_assets", ["asset_id"])
    op.create_index(
        "ix_source_record_assets_deleted_at",
        "source_record_assets",
        ["deleted_at"],
    )
    op.create_index(
        "ix_source_record_assets_source_record_id",
        "source_record_assets",
        ["source_record_id"],
    )
    op.create_index(
        "ix_source_record_assets_source_record_version_id",
        "source_record_assets",
        ["source_record_version_id"],
    )

    op.create_table(
        "field_claims",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", UUID_TYPE, nullable=False),
        sa.Column("field_path", sa.String(length=512), nullable=False),
        sa.Column("value_json", JSON_DOCUMENT, nullable=True),
        sa.Column("value_digest", sa.String(length=64), nullable=False),
        sa.Column("source_record_version_id", UUID_TYPE, nullable=True),
        sa.Column("provider", sa.String(length=127), nullable=True),
        sa.Column("source_pointer", sa.String(length=1024), nullable=True),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("authority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="candidate", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'superseded', 'rejected')",
            name="ck_field_claims_field_claim_status",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_field_claims_field_claim_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_version_id"],
            ["source_record_versions.id"],
            name="fk_field_claims_source_record_version_id_source_record_versions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_field_claims"),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            "value_digest",
            "source_key",
            name="uq_field_claim_evidence",
        ),
    )
    op.create_index(
        "ix_field_claim_entity_field",
        "field_claims",
        ["entity_type", "entity_id", "field_path"],
    )
    op.create_index("ix_field_claims_provider", "field_claims", ["provider"])
    op.create_index(
        "ix_field_claims_source_record_version_id",
        "field_claims",
        ["source_record_version_id"],
    )
    op.create_index("ix_field_claims_status", "field_claims", ["status"])

    op.create_table(
        "field_resolutions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", UUID_TYPE, nullable=False),
        sa.Column("field_path", sa.String(length=512), nullable=False),
        sa.Column("resolution_kind", sa.String(length=16), nullable=False),
        sa.Column("selected_claim_id", UUID_TYPE, nullable=True),
        sa.Column("override_value", JSON_DOCUMENT, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(length=127), nullable=False),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "resolution_kind IN ('claim', 'override', 'cleared')",
            name="ck_field_resolutions_field_resolution_kind",
        ),
        sa.ForeignKeyConstraint(
            ["selected_claim_id"],
            ["field_claims.id"],
            name="fk_field_resolutions_selected_claim_id_field_claims",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_field_resolutions"),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            name="uq_field_resolution_entity_path",
        ),
    )
    op.create_index(
        "ix_field_resolutions_selected_claim_id",
        "field_resolutions",
        ["selected_claim_id"],
    )

    op.create_table(
        "identity_candidates",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("left_asset_id", UUID_TYPE, nullable=False),
        sa.Column("right_asset_id", UUID_TYPE, nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("resolved_by", sa.String(length=127), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected', 'merged')",
            name="ck_identity_candidates_identity_candidate_status",
        ),
        sa.CheckConstraint(
            "left_asset_id <> right_asset_id",
            name="ck_identity_candidates_identity_candidate_non_self",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0.0 AND score <= 1.0)",
            name="ck_identity_candidates_identity_candidate_score",
        ),
        sa.ForeignKeyConstraint(
            ["left_asset_id"],
            ["assets.id"],
            name="fk_identity_candidates_left_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["right_asset_id"],
            ["assets.id"],
            name="fk_identity_candidates_right_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identity_candidates"),
        sa.UniqueConstraint(
            "left_asset_id",
            "right_asset_id",
            "reason",
            name="uq_identity_candidate_pair_reason",
        ),
    )
    op.create_index(
        "ix_identity_candidates_left_asset_id",
        "identity_candidates",
        ["left_asset_id"],
    )
    op.create_index(
        "ix_identity_candidates_right_asset_id",
        "identity_candidates",
        ["right_asset_id"],
    )
    op.create_index("ix_identity_candidates_status", "identity_candidates", ["status"])

    op.create_table(
        "asset_redirects",
        sa.Column("from_asset_id", UUID_TYPE, nullable=False),
        sa.Column("to_asset_id", UUID_TYPE, nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("merge_audit_event_id", UUID_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "from_asset_id <> to_asset_id",
            name="ck_asset_redirects_asset_redirect_non_self",
        ),
        sa.ForeignKeyConstraint(
            ["from_asset_id"],
            ["assets.id"],
            name="fk_asset_redirects_from_asset_id_assets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merge_audit_event_id"],
            ["audit_events.id"],
            name="fk_asset_redirects_merge_audit_event_id_audit_events",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["to_asset_id"],
            ["assets.id"],
            name="fk_asset_redirects_to_asset_id_assets",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("from_asset_id", name="pk_asset_redirects"),
    )
    op.create_index("ix_asset_redirects_to_asset_id", "asset_redirects", ["to_asset_id"])

    op.create_table(
        "representation_mounts",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("representation_id", UUID_TYPE, nullable=False),
        sa.Column("mount_path", sa.String(length=1024), nullable=False),
        sa.Column("source_id", UUID_TYPE, nullable=False),
        sa.Column("root_key", sa.String(length=63), nullable=False),
        sa.Column("path_ref", sa.Text(), nullable=False),
        sa.Column("read_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps_and_version(),
        sa.ForeignKeyConstraint(
            ["representation_id"],
            ["representations.id"],
            name="fk_representation_mounts_representation_id_representations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_representation_mounts_source_id_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_representation_mounts"),
        sa.UniqueConstraint(
            "representation_id",
            "mount_path",
            name="uq_representation_mount_path",
        ),
    )
    op.create_index(
        "ix_representation_mounts_deleted_at",
        "representation_mounts",
        ["deleted_at"],
    )
    op.create_index(
        "ix_representation_mounts_representation_id",
        "representation_mounts",
        ["representation_id"],
    )
    op.create_index(
        "ix_representation_mounts_source_id",
        "representation_mounts",
        ["source_id"],
    )

    op.create_table(
        "relation_evidence",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("relation_id", UUID_TYPE, nullable=False),
        sa.Column("source_record_version_id", UUID_TYPE, nullable=True),
        sa.Column("provider", sa.String(length=127), nullable=True),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_relation_evidence_relation_evidence_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["relation_id"],
            ["relations.id"],
            name="fk_relation_evidence_relation_id_relations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_version_id"],
            ["source_record_versions.id"],
            name="fk_relation_evidence_sr_version",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_relation_evidence"),
        sa.UniqueConstraint(
            "relation_id",
            "evidence_hash",
            name="uq_relation_evidence_hash",
        ),
    )
    op.create_index("ix_relation_evidence_provider", "relation_evidence", ["provider"])
    op.create_index(
        "ix_relation_evidence_relation_id",
        "relation_evidence",
        ["relation_id"],
    )
    op.create_index(
        "ix_relation_evidence_source_record_version_id",
        "relation_evidence",
        ["source_record_version_id"],
    )

    op.create_table(
        "annotation_revisions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("annotation_id", UUID_TYPE, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("anchor", JSON_DOCUMENT, nullable=True),
        sa.Column("editor", sa.String(length=127), nullable=False),
        sa.Column("source", sa.String(length=127), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_annotation_revisions_annotation_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["annotations.id"],
            name="fk_annotation_revisions_annotation_id_annotations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotation_revisions"),
        sa.UniqueConstraint(
            "annotation_id",
            "revision",
            name="uq_annotation_revision_number",
        ),
    )
    op.create_index(
        "ix_annotation_revisions_annotation_id",
        "annotation_revisions",
        ["annotation_id"],
    )

    _backfill_source_versions_and_assets()


def downgrade() -> None:
    op.drop_table("annotation_revisions")
    op.drop_table("relation_evidence")
    op.drop_table("representation_mounts")
    op.drop_table("asset_redirects")
    op.drop_table("identity_candidates")
    op.drop_table("field_resolutions")
    op.drop_table("field_claims")
    op.drop_table("source_record_assets")
    op.drop_table("source_record_versions")
    op.drop_table("repository_profiles")
    op.drop_table("paper_profiles")

    with op.batch_alter_table("annotations") as batch:
        batch.drop_constraint(
            "ck_annotations_annotation_anchor_status",
            type_="check",
        )
        batch.drop_constraint(
            "ck_annotations_annotation_anchor_kind",
            type_="check",
        )
        batch.drop_column("anchor_status")
        batch.drop_column("anchor_digest")
        batch.drop_column("anchor_kind")

    op.drop_index("ix_representations_tree_digest", table_name="representations")
    op.drop_index("ix_representations_revision", table_name="representations")
    op.drop_index("ix_representations_availability", table_name="representations")
    op.drop_index("ix_representations_source_record_id", table_name="representations")
    with op.batch_alter_table("representations") as batch:
        batch.drop_constraint(
            "uq_representation_asset_source_path_role",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_representation_source_path_role",
            ["source_id", "root_key", "path_ref", "role"],
        )
        batch.drop_constraint(
            "ck_representations_representation_availability",
            type_="check",
        )
        batch.drop_constraint(
            "ck_representations_representation_materialization_kind",
            type_="check",
        )
        batch.drop_constraint(
            "fk_representations_source_record_id_source_records",
            type_="foreignkey",
        )
        batch.drop_column("verified_at")
        batch.drop_column("tree_digest")
        batch.drop_column("revision")
        batch.drop_column("entrypoint")
        batch.drop_column("availability")
        batch.drop_column("materialization_kind")
        batch.drop_column("source_record_id")

    op.drop_index(
        "uq_external_identifier_identity",
        table_name="external_identifiers",
    )
    with op.batch_alter_table("external_identifiers") as batch:
        batch.drop_constraint("uq_external_identifier_per_asset", type_="unique")
        batch.create_unique_constraint(
            "uq_external_identifier_authority",
            ["scheme", "canonical"],
        )
        batch.create_unique_constraint(
            "uq_external_identifier_per_asset",
            ["asset_id", "scheme", "canonical"],
        )
        batch.drop_column("is_identity")
        batch.drop_column("scope")
