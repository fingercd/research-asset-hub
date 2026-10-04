"""Versioned, path-free portable research package manifest."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.workflows.contracts import WorkflowContract


class PackageIdentifier(WorkflowContract):
    scheme: str = Field(pattern="^[a-z][a-z0-9_-]{0,31}$")
    scope: str = Field(default="", max_length=255)
    value: str = Field(min_length=1, max_length=4096)
    canonical: str = Field(min_length=1, max_length=4096)
    is_identity: bool = True


class PackageAsset(WorkflowContract):
    id: UUID
    asset_type: str = Field(min_length=1, max_length=65)
    title: str = Field(min_length=1, max_length=10000)
    title_zh: str | None = None
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list, max_length=10000)
    year: int | None = Field(default=None, ge=1450, le=2200)
    venue: str | None = None
    language: str | None = None
    abstract: str | None = Field(default=None, max_length=2_000_000)
    summary: str | None = Field(default=None, max_length=2_000_000)
    domain_slugs: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=1000)
    source_urls: list[str] = Field(default_factory=list, max_length=1000)
    identifiers: list[PackageIdentifier] = Field(default_factory=list, max_length=1000)


class PackageFile(WorkflowContract):
    path: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class PackageRepresentation(WorkflowContract):
    id: UUID
    asset_id: UUID
    representation_type: RepresentationType
    role: RepresentationRole
    materialization_kind: Literal["file", "directory", "url", "bundle"]
    external_url: str | None = Field(default=None, max_length=4096)
    entrypoint: str | None = None
    revision: str | None = Field(default=None, max_length=255)
    sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    tree_digest: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    mime_type: str | None = Field(default=None, max_length=255)
    files: list[PackageFile] = Field(default_factory=list)
    omission_reason: str | None = None


class PackageRelation(WorkflowContract):
    id: UUID
    subject_asset_id: UUID
    object_asset_id: UUID
    relation_type: RelationType
    confirmation_status: Literal["candidate", "confirmed", "rejected"]
    confidence: float | None = Field(default=None, ge=0, le=1)


class PackageDomain(WorkflowContract):
    slug: str = Field(pattern="^[a-z0-9][a-z0-9-]{1,63}$")
    label: str = Field(min_length=1, max_length=255)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class PackageAnnotation(WorkflowContract):
    id: UUID
    asset_id: UUID
    representation_id: UUID | None = None
    kind: Literal["general", "comment", "summary", "question", "highlight"]
    title: str | None = None
    summary: str | None = None
    body_markdown: str = Field(max_length=2_000_000)
    anchor: dict[str, Any] | None = None
    anchor_kind: (
        Literal["pdf_page", "html_heading", "text_lines", "code_lines", "notebook_cell"] | None
    ) = None
    anchor_status: Literal["current", "stale", "needs_review", "orphaned"] = "current"
    pinned: bool = False


class PackageReadingState(WorkflowContract):
    asset_id: UUID
    status: Literal["unread", "reading", "read"]


class PackageReadingPosition(WorkflowContract):
    asset_id: UUID
    representation_id: UUID
    page: int = Field(ge=1)
    source_hash: str = Field(pattern="^[a-f0-9]{64}$")


class PackageUserState(WorkflowContract):
    reading_states: list[PackageReadingState] = Field(default_factory=list)
    reading_positions: list[PackageReadingPosition] = Field(default_factory=list)
    annotations: list[PackageAnnotation] = Field(default_factory=list)


class PackageManifest(WorkflowContract):
    schema_version: Literal[1] = 1
    format: Literal["research-asset-hub-domain-package"] = "research-asset-hub-domain-package"
    assets: list[PackageAsset] = Field(min_length=1, max_length=100000)
    representations: list[PackageRepresentation] = Field(default_factory=list, max_length=100000)
    relations: list[PackageRelation] = Field(default_factory=list, max_length=1000000)
    domains: list[PackageDomain] = Field(default_factory=list, max_length=10000)
    user_state: PackageUserState | None = None
    skipped_files: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self) -> Self:
        asset_ids = {item.id for item in self.assets}
        representation_ids = {item.id for item in self.representations}
        if len(asset_ids) != len(self.assets) or len(representation_ids) != len(
            self.representations
        ):
            raise ValueError("package identities must be unique")
        if any(item.asset_id not in asset_ids for item in self.representations):
            raise ValueError("representation owner is outside the package")
        for representation in self.representations:
            if representation.materialization_kind == "file" and representation.files:
                if len(representation.files) != 1:
                    raise ValueError("file representations must have exactly one attachment")
                if (
                    representation.sha256
                    and representation.sha256 != representation.files[0].sha256
                ):
                    raise ValueError("representation and attachment digests disagree")
        if any(
            item.subject_asset_id not in asset_ids
            or item.object_asset_id not in asset_ids
            or item.subject_asset_id == item.object_asset_id
            for item in self.relations
        ):
            raise ValueError("relation endpoints must be distinct package assets")
        paths = [
            file.path for representation in self.representations for file in representation.files
        ]
        if len(set(paths)) != len(paths):
            raise ValueError("attachment paths must be unique")
        if any(
            file.path.split("/")[:2] != ["attachments", str(representation.id)]
            for representation in self.representations
            for file in representation.files
        ):
            raise ValueError("attachments must use their representation identity directory")
        if self.user_state:
            user_asset_ids = (
                {state.asset_id for state in self.user_state.reading_states}
                | {position.asset_id for position in self.user_state.reading_positions}
                | {annotation.asset_id for annotation in self.user_state.annotations}
            )
            if not user_asset_ids <= asset_ids:
                raise ValueError("user state owner is outside the package")
            owners = {item.id: item.asset_id for item in self.representations}
            if any(
                owners.get(item.representation_id) != item.asset_id
                for item in self.user_state.reading_positions
            ):
                raise ValueError("reading position representation belongs to another asset")
            if any(
                item.representation_id is not None
                and owners.get(item.representation_id) != item.asset_id
                for item in self.user_state.annotations
            ):
                raise ValueError("annotation representation belongs to another asset")
        return self


class PackageCommitRequest(WorkflowContract):
    identity_mapping: dict[UUID, UUID | None] = Field(default_factory=dict)
    include_user_state: bool = False
