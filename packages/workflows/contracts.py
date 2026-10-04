from __future__ import annotations

from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkflowContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionCreate(WorkflowContract):
    label: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label cannot be blank")
        return value.strip()


class CollectionPatch(WorkflowContract):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one change is required")
        if "label" in self.model_fields_set:
            if self.label is None or not self.label.strip():
                raise ValueError("label cannot be null or blank")
            self.label = self.label.strip()
        return self


class AssetSelection(WorkflowContract):
    asset_ids: list[UUID] = Field(min_length=1, max_length=1000)

    @field_validator("asset_ids")
    @classmethod
    def unique_assets(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class BulkUpdate(AssetSelection):
    reading_status: Literal["unread", "reading", "read"] | None = None
    add_tags: list[str] = Field(default_factory=list, max_length=100)
    remove_tags: list[str] = Field(default_factory=list, max_length=100)
    add_domains: list[str] = Field(default_factory=list, max_length=100)
    remove_domains: list[str] = Field(default_factory=list, max_length=100)
    collection_id: UUID | None = None
    collection_action: Literal["add", "remove"] | None = None

    @field_validator("add_tags", "remove_tags", "add_domains", "remove_domains")
    @classmethod
    def clean_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() or len(name) > 255 for name in value):
            raise ValueError("names must contain 1–255 nonblank characters")
        return list(dict.fromkeys(name.strip() for name in value))

    @model_validator(mode="after")
    def meaningful_change(self) -> Self:
        if (self.collection_id is None) != (self.collection_action is None):
            raise ValueError("collection_id and collection_action must be supplied together")
        if not any(
            (
                self.reading_status is not None,
                self.add_tags,
                self.remove_tags,
                self.add_domains,
                self.remove_domains,
                self.collection_id,
            )
        ):
            raise ValueError("at least one operation is required")
        return self


class SearchDefinition(WorkflowContract):
    q: str = Field(default="", max_length=10_000)
    mode: Literal["exact", "keyword"] = "keyword"
    sort: Literal["date_desc", "date_asc", "relevance"] = "date_desc"
    group_by: Literal["paper", "none"] = "paper"
    domain: str | None = None
    status: Literal["unread", "reading", "read", ""] | None = None
    author: str | None = None
    type: str | None = None
    year_from: int | None = Field(default=None, ge=1450, le=2200)
    year_to: int | None = Field(default=None, ge=1450, le=2200)
    date_from: date | None = None
    date_to: date | None = None
    tag: str | None = None
    has_pdf: bool | None = None
    has_html: bool | None = None
    has_code: bool | None = None
    has_guide: bool | None = None
    has_annotation: bool | None = None
    collection_id: UUID | None = None

    @model_validator(mode="after")
    def ordered_ranges(self) -> Self:
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must not exceed year_to")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from must not exceed date_to")
        return self


class SavedSearchCreate(WorkflowContract):
    name: str = Field(min_length=1, max_length=255)
    definition: SearchDefinition

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()


class SavedSearchPatch(WorkflowContract):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    definition: SearchDefinition | None = None

    @model_validator(mode="after")
    def changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one change is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("saved search fields cannot be null")
        if self.name is not None:
            if not self.name.strip():
                raise ValueError("name cannot be blank")
            self.name = self.name.strip()
        return self


class ReadingPositionUpdate(WorkflowContract):
    representation_id: UUID
    page: int = Field(ge=1)
    source_hash: str | None = Field(default=None, pattern="^[a-fA-F0-9]{64}$")


class PackageExportRequest(WorkflowContract):
    domain: str | None = None
    asset_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100_000)
    include_user_state: bool = False
    include_external_files: bool = True

    @model_validator(mode="after")
    def explicit_scope(self) -> Self:
        if not self.domain and not self.asset_ids:
            raise ValueError("domain or asset_ids is required")
        return self
