from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from packages.domain.schemas import ExternalIdentifier, _validate_postgres_text


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=2000)
    title_zh: str | None = None
    abstract: str | None = None
    summary: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1450, le=2200)
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    html_url: HttpUrl | None = None

    @field_validator("title", "doi", "arxiv_id")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("doi", "arxiv_id")
    @classmethod
    def valid_identifier(cls, value: str | None, info: Any) -> str | None:
        if value is not None:
            ExternalIdentifier(scheme="doi" if info.field_name == "doi" else "arxiv", value=value)
        return value

    @model_validator(mode="after")
    def postgres_text(self) -> Metadata:
        _validate_postgres_text(self.model_dump(mode="json"))
        return self


class DraftCreate(Metadata):
    domain: str = Field(min_length=1, max_length=64)
    storage_mode: Literal["managed", "reference"] = "managed"


class DraftPatch(Metadata):
    domain: str | None = Field(default=None, min_length=1, max_length=64)
    reference_path: str | None = None


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "attach"] = "create"
    asset_id: UUID | None = None
