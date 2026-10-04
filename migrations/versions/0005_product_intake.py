"""Add persistent domains and recoverable PDF intake drafts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_product_intake"
down_revision: str | None = "0004_source_retirement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "domain_definitions",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("aliases", JSON_DOCUMENT, nullable=False),
        sa.Column("tags", JSON_DOCUMENT, nullable=False),
        *timestamps(),
    )
    op.create_table(
        "intake_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("storage_mode", sa.String(16), nullable=False),
        sa.Column("metadata_json", JSON_DOCUMENT, nullable=False),
        sa.Column("filename", sa.String(255)),
        sa.Column("reference_path", sa.Text()),
        sa.Column("pdf_sha256", sa.String(64)),
        sa.Column("pdf_size_bytes", sa.BigInteger()),
        sa.Column("commit_plan", JSON_DOCUMENT),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="RESTRICT")),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("sources.id", ondelete="RESTRICT")),
        sa.Column("error", sa.Text()),
        *timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'committing', 'committed', 'cancelled')",
            name="intake_status",
        ),
        sa.CheckConstraint("storage_mode IN ('managed', 'reference')", name="intake_storage_mode"),
    )


def downgrade() -> None:
    op.drop_table("intake_drafts")
    op.drop_table("domain_definitions")
