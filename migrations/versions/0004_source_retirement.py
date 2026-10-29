"""Add immutable source-retirement, successor, mapping and rollback evidence.

Revision ID: 0004_source_retirement
Revises: 0003_search_rag_integrations
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_source_retirement"
down_revision: str | None = "0003_search_rag_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID_TYPE = sa.Uuid()
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "source_retirements",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_id", UUID_TYPE, nullable=False),
        sa.Column("backup_id", UUID_TYPE, nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("frozen_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_source_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("record_set_digest", sa.String(length=64), nullable=False),
        sa.Column("representation_set_digest", sa.String(length=64), nullable=False),
        sa.Column("mount_set_digest", sa.String(length=64), nullable=False),
        sa.Column("collection_digest", sa.String(length=64), nullable=False),
        sa.Column("signature_algorithm", sa.String(length=32), nullable=False),
        sa.Column("signer_key_id", sa.String(length=255), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("applied_by", sa.String(length=127), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["backup_id"],
            ["backup_runs.id"],
            name="fk_source_retirements_backup_id_backup_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_source_retirements_source_id_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_retirements"),
        sa.UniqueConstraint("payload_digest", name="uq_source_retirement_payload"),
    )
    op.create_index("ix_source_retirements_applied_at", "source_retirements", ["applied_at"])
    op.create_index("ix_source_retirements_backup_id", "source_retirements", ["backup_id"])
    op.create_index("ix_source_retirements_source_id", "source_retirements", ["source_id"])

    op.create_table(
        "source_retirement_record_maps",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("retirement_id", UUID_TYPE, nullable=False),
        sa.Column("retired_source_record_id", UUID_TYPE, nullable=False),
        sa.Column("successor_source_record_id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=True),
        sa.Column("retired_record_digest", sa.String(length=64), nullable=False),
        sa.Column("successor_record_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_sr_record_maps_asset_id_assets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_source_record_id"],
            ["source_records.id"],
            name="fk_sr_record_maps_retired_source_record_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirement_id"],
            ["source_retirements.id"],
            name="fk_sr_record_maps_retirement_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_source_record_id"],
            ["source_records.id"],
            name="fk_sr_record_maps_successor_source_record_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_retirement_record_maps"),
        sa.UniqueConstraint(
            "retirement_id",
            "retired_source_record_id",
            name="uq_sr_record_retired",
        ),
        sa.UniqueConstraint(
            "retirement_id",
            "successor_source_record_id",
            name="uq_sr_record_successor",
        ),
    )
    op.create_index(
        "ix_source_retirement_record_maps_asset_id",
        "source_retirement_record_maps",
        ["asset_id"],
    )
    op.create_index(
        "ix_source_retirement_record_maps_retired_source_record_id",
        "source_retirement_record_maps",
        ["retired_source_record_id"],
    )
    op.create_index(
        "ix_source_retirement_record_maps_retirement_id",
        "source_retirement_record_maps",
        ["retirement_id"],
    )
    op.create_index(
        "ix_source_retirement_record_maps_successor_source_record_id",
        "source_retirement_record_maps",
        ["successor_source_record_id"],
    )

    op.create_table(
        "source_retirement_representation_maps",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("retirement_id", UUID_TYPE, nullable=False),
        sa.Column("retired_representation_id", UUID_TYPE, nullable=False),
        sa.Column("successor_representation_id", UUID_TYPE, nullable=False),
        sa.Column("asset_id", UUID_TYPE, nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("retired_representation_digest", sa.String(length=64), nullable=False),
        sa.Column("successor_representation_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_sr_rep_maps_asset_id_assets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_representation_id"],
            ["representations.id"],
            name="fk_sr_rep_maps_retired_representation_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirement_id"],
            ["source_retirements.id"],
            name="fk_sr_rep_maps_retirement_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_representation_id"],
            ["representations.id"],
            name="fk_sr_rep_maps_successor_representation_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_retirement_representation_maps"),
        sa.UniqueConstraint(
            "retirement_id",
            "retired_representation_id",
            name="uq_sr_rep_retired",
        ),
        sa.UniqueConstraint(
            "retirement_id",
            "successor_representation_id",
            name="uq_sr_rep_successor",
        ),
    )
    op.create_index(
        "ix_source_retirement_representation_maps_asset_id",
        "source_retirement_representation_maps",
        ["asset_id"],
    )
    op.create_index(
        "ix_sr_rep_map_retired_representation",
        "source_retirement_representation_maps",
        ["retired_representation_id"],
    )
    op.create_index(
        "ix_source_retirement_representation_maps_retirement_id",
        "source_retirement_representation_maps",
        ["retirement_id"],
    )
    op.create_index(
        "ix_sr_rep_map_successor_representation",
        "source_retirement_representation_maps",
        ["successor_representation_id"],
    )

    op.create_table(
        "source_retirement_mount_maps",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("retirement_id", UUID_TYPE, nullable=False),
        sa.Column("retired_mount_id", UUID_TYPE, nullable=False),
        sa.Column("successor_mount_id", UUID_TYPE, nullable=False),
        sa.Column("retired_representation_id", UUID_TYPE, nullable=False),
        sa.Column("successor_representation_id", UUID_TYPE, nullable=False),
        sa.Column("retired_mount_digest", sa.String(length=64), nullable=False),
        sa.Column("successor_mount_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["retired_mount_id"],
            ["representation_mounts.id"],
            name="fk_sr_mount_maps_retired_mount_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_representation_id"],
            ["representations.id"],
            name="fk_sr_mount_maps_retired_representation_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirement_id"],
            ["source_retirements.id"],
            name="fk_sr_mount_maps_retirement_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_mount_id"],
            ["representation_mounts.id"],
            name="fk_sr_mount_maps_successor_mount_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_representation_id"],
            ["representations.id"],
            name="fk_sr_mount_maps_successor_representation_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_retirement_mount_maps"),
        sa.UniqueConstraint("retirement_id", "retired_mount_id", name="uq_sr_mount_retired"),
        sa.UniqueConstraint(
            "retirement_id", "successor_mount_id", name="uq_sr_mount_successor"
        ),
    )
    op.create_index(
        "ix_source_retirement_mount_maps_retired_mount_id",
        "source_retirement_mount_maps",
        ["retired_mount_id"],
    )
    op.create_index(
        "ix_source_retirement_mount_maps_retirement_id",
        "source_retirement_mount_maps",
        ["retirement_id"],
    )
    op.create_index(
        "ix_source_retirement_mount_maps_successor_mount_id",
        "source_retirement_mount_maps",
        ["successor_mount_id"],
    )

    op.create_table(
        "source_retirement_successors",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("retirement_id", UUID_TYPE, nullable=False),
        sa.Column("successor_source_id", UUID_TYPE, nullable=False),
        sa.Column("frozen_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_source_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_state_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["retirement_id"],
            ["source_retirements.id"],
            name="fk_sr_successors_retirement_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_source_id"],
            ["sources.id"],
            name="fk_sr_successors_successor_source_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_retirement_successors"),
        sa.UniqueConstraint(
            "retirement_id", "successor_source_id", name="uq_sr_successor_source"
        ),
    )
    op.create_index(
        "ix_source_retirement_successors_retirement_id",
        "source_retirement_successors",
        ["retirement_id"],
    )
    op.create_index(
        "ix_source_retirement_successors_successor_source_id",
        "source_retirement_successors",
        ["successor_source_id"],
    )

    op.create_table(
        "source_retirement_rollbacks",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("retirement_id", UUID_TYPE, nullable=False),
        sa.Column("backup_id", UUID_TYPE, nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("signature_algorithm", sa.String(length=32), nullable=False),
        sa.Column("signer_key_id", sa.String(length=255), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rolled_back_by", sa.String(length=127), nullable=False),
        sa.Column(
            "rolled_back_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["backup_id"],
            ["backup_runs.id"],
            name="fk_sr_rollbacks_backup_id_backup_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirement_id"],
            ["source_retirements.id"],
            name="fk_sr_rollbacks_retirement_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_retirement_rollbacks"),
        sa.UniqueConstraint("payload_digest", name="uq_sr_rollback_payload"),
        sa.UniqueConstraint("retirement_id", name="uq_sr_rollback_retirement"),
    )
    op.create_index(
        "ix_source_retirement_rollbacks_backup_id",
        "source_retirement_rollbacks",
        ["backup_id"],
    )
    op.create_index(
        "ix_source_retirement_rollbacks_retirement_id",
        "source_retirement_rollbacks",
        ["retirement_id"],
    )
    op.create_index(
        "ix_source_retirement_rollbacks_rolled_back_at",
        "source_retirement_rollbacks",
        ["rolled_back_at"],
    )


def downgrade() -> None:
    op.drop_table("source_retirement_rollbacks")
    op.drop_table("source_retirement_successors")
    op.drop_table("source_retirement_mount_maps")
    op.drop_table("source_retirement_representation_maps")
    op.drop_table("source_retirement_record_maps")
    op.drop_table("source_retirements")
