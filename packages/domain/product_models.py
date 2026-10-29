"""User-owned domain definitions and recoverable manual intake drafts."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .identifiers import new_uuid7
from .models import JSON_DOCUMENT, Base, TimestampVersionMixin


class DomainDefinition(TimestampVersionMixin, Base):
    __tablename__ = "domain_definitions"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)


class IntakeDraft(TimestampVersionMixin, Base):
    __tablename__ = "intake_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'ready', 'committing', 'committed', 'cancelled')",
            name="intake_status",
        ),
        CheckConstraint("storage_mode IN ('managed', 'reference')", name="intake_storage_mode"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    storage_mode: Mapped[str] = mapped_column(String(16), default="managed", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )
    filename: Mapped[str | None] = mapped_column(String(255))
    reference_path: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    pdf_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    commit_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    error: Mapped[str | None] = mapped_column(Text)
