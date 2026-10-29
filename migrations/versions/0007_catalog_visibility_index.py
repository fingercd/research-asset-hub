"""Index visible catalog rows without repeatedly decompressing audit attributes.

Revision ID: 0007_catalog_visibility_index
Revises: 0006_research_workflows
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_catalog_visibility_index"
down_revision: str | None = "0006_research_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_assets_visible_catalog",
        "assets",
        ["id"],
        postgresql_include=["asset_type", "updated_at"],
        postgresql_where=sa.text(
            "deleted_at IS NULL AND NOT coalesce("
            "lower(attributes->>'system_hidden') = 'true', false)"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_assets_visible_catalog", "assets")
