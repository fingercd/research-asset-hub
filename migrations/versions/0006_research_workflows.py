"""Persist reading positions and portable research package artifacts.

Revision ID: 0006_research_workflows
Revises: 0005_product_intake
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_research_workflows"
down_revision: str | None = "0005_product_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "reading_positions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.String(127), nullable=False),
        sa.Column(
            "asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "representation_id",
            sa.Uuid(),
            sa.ForeignKey("representations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "asset_id", name="uq_reading_position_user_asset"),
        sa.CheckConstraint("page >= 1", name="ck_reading_positions_reading_position_page"),
    )
    op.create_index("ix_reading_positions_asset_id", "reading_positions", ["asset_id"])
    op.create_index(
        "ix_reading_positions_representation_id", "reading_positions", ["representation_id"]
    )
    op.create_table(
        "package_exports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("manifest", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "package_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("manifest", JSON_DOCUMENT, nullable=False),
        sa.Column("preview", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", JSON_DOCUMENT, nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("sha256", name="uq_package_import_sha256"),
        sa.CheckConstraint(
            "status IN ('preview', 'committed')", name="ck_package_imports_package_import_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("package_imports")
    op.drop_table("package_exports")
    op.drop_index("ix_reading_positions_representation_id", "reading_positions")
    op.drop_index("ix_reading_positions_asset_id", "reading_positions")
    op.drop_table("reading_positions")
