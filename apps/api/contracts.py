from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.domain.enums import ConfirmationStatus, RelationType

AnnotationKindValue = Literal["general", "comment", "summary", "question", "highlight"]
AnchorKindValue = Literal[
    "pdf_page",
    "html_heading",
    "text_lines",
    "code_lines",
    "notebook_cell",
]


class AssetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=10_000)
    asset_type: str = Field(default="paper", min_length=1, max_length=65)
    title_zh: str | None = None
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1450, le=2200)
    venue: str | None = None
    language: str | None = None
    abstract: str | None = None
    summary: str | None = None
    content: str | None = None
    domain_slugs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    visibility: Literal["private", "internal", "public"] = "private"
    analysis: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AssetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    asset_type: str | None = None
    title_zh: str | None = None
    subtitle: str | None = None
    authors: list[str] | None = None
    year: int | None = Field(default=None, ge=1450, le=2200)
    venue: str | None = None
    language: str | None = None
    abstract: str | None = None
    summary: str | None = None
    content: str | None = None
    domain_slugs: list[str] | None = None
    tags: list[str] | None = None
    source_urls: list[str] | None = None
    visibility: Literal["private", "internal", "public"] | None = None
    analysis: dict[str, Any] | None = None
    attributes: dict[str, Any] | None = None

    @model_validator(mode="after")
    def reject_null_for_required_columns(self) -> Self:
        non_nullable = {
            "title",
            "asset_type",
            "authors",
            "domain_slugs",
            "tags",
            "source_urls",
            "visibility",
            "analysis",
            "attributes",
        }
        for field_name in non_nullable & self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class ReadingStateUpdate(BaseModel):
    status: Literal["unread", "reading", "read"]


class AnnotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID
    representation_id: UUID | None = None
    kind: AnnotationKindValue = "comment"
    title: str | None = None
    summary: str | None = None
    body_markdown: str = Field(default="", max_length=2_000_000)
    anchor: dict[str, Any] | None = None
    anchor_kind: AnchorKindValue | None = None
    pinned: bool = False


class AnnotationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    representation_id: UUID | None = None
    kind: AnnotationKindValue | None = None
    title: str | None = None
    summary: str | None = None
    body_markdown: str | None = Field(default=None, max_length=2_000_000)
    anchor: dict[str, Any] | None = None
    anchor_kind: AnchorKindValue | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def reject_null_for_required_columns(self) -> Self:
        for field_name in {"kind", "body_markdown", "pinned"} & self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class RelationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_asset_id: UUID
    object_asset_id: UUID
    relation_type: RelationType
    source_key: str = Field(default="manual", min_length=1, max_length=255)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confirmation_status: ConfirmationStatus = ConfirmationStatus.CANDIDATE
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_self_relation(self) -> Self:
        if self.subject_asset_id == self.object_asset_id:
            raise ValueError("subject_asset_id and object_asset_id must be different")
        return self


class RelationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_asset_id: UUID | None = None
    object_asset_id: UUID | None = None
    relation_type: RelationType | None = None
    source_key: str | None = Field(default=None, min_length=1, max_length=255)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confirmation_status: ConfirmationStatus | None = None
    attributes: dict[str, Any] | None = None

    @model_validator(mode="after")
    def reject_null_for_required_columns(self) -> Self:
        non_nullable = {
            "subject_asset_id",
            "object_asset_id",
            "relation_type",
            "source_key",
            "confirmation_status",
            "attributes",
        }
        for field_name in non_nullable & self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class RelationEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_version_id: UUID | None = None
    provider: str | None = Field(default=None, max_length=127)
    source_key: str = Field(default="manual", min_length=1, max_length=255)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    fetched_at: datetime | None = None


class OverrideCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["asset"] = "asset"
    entity_id: UUID
    field_path: str = Field(min_length=2, max_length=512)
    override_value: Any = None
    reason: str | None = None

    @field_validator("field_path")
    @classmethod
    def field_path_is_json_pointer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/") or normalized == "/":
            raise ValueError("field_path must be a non-root JSON Pointer")
        return normalized


class OverridePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_value: Any = None
    reason: str | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self


class ComparisonCreate(BaseModel):
    asset_ids: list[UUID] = Field(min_length=2, max_length=4)
    title: str | None = None


class ConflictResolutionRequest(BaseModel):
    strategy: Literal["keep_local", "accept_incoming", "merge"]
    resolved_value: Any | None = None


class SyncApplyRequest(BaseModel):
    preview_id: UUID


class ListResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    total: int | None = None
