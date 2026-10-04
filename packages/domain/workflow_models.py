"""User workflow state and registered package artifacts.

These records never change immutable source records or source files.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.domain.identifiers import new_uuid7
from packages.domain.models import JSON_DOCUMENT, Base, TimestampVersionMixin


class ReadingPosition(TimestampVersionMixin, Base):
    __tablename__ = "reading_positions"
    __table_args__ = (
        UniqueConstraint("user_id", "asset_id", name="uq_reading_position_user_asset"),
        CheckConstraint("page >= 1", name="reading_position_page"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    user_id: Mapped[str] = mapped_column(String(127), default="local", nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    representation_id: Mapped[UUID] = mapped_column(
        ForeignKey("representations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PackageExport(TimestampVersionMixin, Base):
    __tablename__ = "package_exports"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)


class PackageImport(TimestampVersionMixin, Base):
    __tablename__ = "package_imports"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_package_import_sha256"),
        CheckConstraint("status IN ('preview', 'committed')", name="package_import_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="preview", nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
