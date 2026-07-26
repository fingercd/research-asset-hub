"""Strict contracts for curated domain portal documents."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ReviewStatus = Literal["generated", "reviewed", "not_applicable", "needs_review"]


class PortalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        str_strip_whitespace=True,
    )


class SourceSelector(PortalModel):
    source_id: Slug
    source_record_id: NonEmpty


class SourceInput(PortalModel):
    path: NonEmpty
    sha256: Digest
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _relative_safe_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith(("/", "//"))
            or re.match(r"^[A-Za-z]:/", normalized)
            or "\x00" in normalized
            or ".." in normalized.split("/")
        ):
            raise ValueError("source input paths must be safe relative paths")
        return normalized


class PortalRegistryItem(PortalModel):
    slug: Slug
    label: NonEmpty
    content_directory: NonEmpty
    original_selector: SourceSelector
    allowed_mounts: list[NonEmpty] = Field(min_length=1)
    default_view: Literal["overview", "units", "recent", "stats"] = "overview"

    @field_validator("content_directory")
    @classmethod
    def _content_directory_is_safe(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith(("/", "//"))
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in normalized.split("/")
        ):
            raise ValueError("content_directory must be relative and contained")
        return normalized.rstrip("/")

    @field_validator("allowed_mounts")
    @classmethod
    def _mounts_are_absolute_virtual_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            mount = value.rstrip("/") or "/"
            if not mount.startswith("/") or ".." in mount.split("/"):
                raise ValueError("allowed mounts must be safe virtual paths")
            normalized.append(mount)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed mounts must be unique")
        return normalized


class PortalIndex(PortalModel):
    schema_version: Literal["research-asset-hub.domain-portal-index/v2"]
    portals: list[PortalRegistryItem] = Field(min_length=1)

    @field_validator("portals")
    @classmethod
    def _portal_slugs_are_unique(
        cls, values: list[PortalRegistryItem]
    ) -> list[PortalRegistryItem]:
        slugs = [item.slug for item in values]
        if len(slugs) != len(set(slugs)):
            raise ValueError("portal slugs must be unique")
        return values


class PortalMetadata(PortalModel):
    schema_version: Literal["research-asset-hub.domain-portal-config/v2"]
    slug: Slug
    label: NonEmpty
    description: NonEmpty
    content_version: NonEmpty
    review_status: ReviewStatus
    original_selector: SourceSelector
    source_inputs: list[SourceInput] = Field(min_length=1)
    section_evidence: dict[NonEmpty, list[NonEmpty]]
    appendices: list[dict[str, object]] = Field(default_factory=list)


class TimelineStage(PortalModel):
    id: Slug
    title: NonEmpty
    period: NonEmpty
    summary: NonEmpty
    asset_ids: list[NonEmpty] = Field(min_length=1)
    status: ReviewStatus = "reviewed"


class TimelineDocument(PortalModel):
    schema_version: Literal["research-asset-hub.domain-portal-timeline/v2"]
    stages: list[TimelineStage] = Field(min_length=2)


class CapabilityItem(PortalModel):
    id: Slug
    title: NonEmpty
    summary: NonEmpty


class CapabilityMethodCell(PortalModel):
    capability_id: Slug
    method_ids: list[Slug] = Field(min_length=1)
    summary: NonEmpty
    asset_ids: list[NonEmpty] = Field(min_length=1)


class CapabilityMethodDocument(PortalModel):
    schema_version: Literal[
        "research-asset-hub.domain-portal-capability-method-map/v2"
    ]
    capabilities: list[CapabilityItem] = Field(min_length=3)
    methods: list[CapabilityItem] = Field(min_length=3)
    cells: list[CapabilityMethodCell] = Field(min_length=3)

    @field_validator("capabilities", "methods")
    @classmethod
    def _item_ids_are_unique(
        cls, values: list[CapabilityItem]
    ) -> list[CapabilityItem]:
        identifiers = [item.id for item in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("capability and method identifiers must be unique")
        return values


class LandmarkItem(PortalModel):
    id: Slug
    title: NonEmpty
    summary: NonEmpty
    asset_id: NonEmpty
    status: ReviewStatus = "reviewed"


class LandmarkDocument(PortalModel):
    schema_version: Literal["research-asset-hub.domain-portal-landmarks/v2"]
    items: list[LandmarkItem] = Field(min_length=3)


class ReadingStep(PortalModel):
    order: int = Field(ge=1)
    asset_id: NonEmpty
    title: NonEmpty
    rationale: NonEmpty


class ReadingRoute(PortalModel):
    id: Slug
    title: NonEmpty
    goal: NonEmpty
    prerequisites: list[NonEmpty]
    steps: list[ReadingStep] = Field(min_length=3, max_length=12)


class ReadingRouteDocument(PortalModel):
    schema_version: Literal["research-asset-hub.domain-portal-reading-routes/v2"]
    routes: list[ReadingRoute] = Field(min_length=3, max_length=5)


class BenchmarkRow(PortalModel):
    id: Slug
    task: NonEmpty
    datasets_or_environments: list[NonEmpty] = Field(min_length=1)
    metrics: list[NonEmpty] = Field(min_length=1)
    caution: NonEmpty
    asset_ids: list[NonEmpty] = Field(min_length=1)


class BenchmarkDocument(PortalModel):
    schema_version: Literal["research-asset-hub.domain-portal-benchmarks/v2"]
    status: ReviewStatus
    rows: list[BenchmarkRow]
    not_applicable_reason: str | None = None


class PortalSection(PortalModel):
    id: Literal[
        "scope",
        "timeline",
        "capability-method-map",
        "landmarks",
        "benchmarks",
        "reading-routes",
        "catalog",
        "frontiers",
        "evidence",
    ]
    kind: Literal[
        "scope",
        "timeline",
        "capability_method_map",
        "landmarks",
        "benchmarks",
        "reading_routes",
        "catalog",
        "frontiers",
        "evidence",
    ]
    title: NonEmpty
    status: ReviewStatus
    content: dict[str, object]


class PortalStaleReason(PortalModel):
    path: NonEmpty
    reason: Literal["missing", "digest_changed", "size_changed", "mtime_changed"]
    expected: str | int | None = None
    actual: str | int | None = None


class DomainPortalDocument(PortalModel):
    schema_id: Literal["research-asset-hub.domain-portal/v2"] = Field(alias="schema")
    slug: Slug
    label: NonEmpty
    description: NonEmpty
    content_version: NonEmpty
    content_digest: Digest
    review_status: ReviewStatus
    stale: bool
    stale_reasons: list[PortalStaleReason]
    original_selector: SourceSelector
    sections: list[PortalSection]

    @field_validator("sections")
    @classmethod
    def _sections_are_complete_and_ordered(
        cls, values: list[PortalSection]
    ) -> list[PortalSection]:
        expected = [
            "scope",
            "timeline",
            "capability-method-map",
            "landmarks",
            "benchmarks",
            "reading-routes",
            "catalog",
            "frontiers",
            "evidence",
        ]
        actual = [item.id for item in values]
        if actual != expected:
            raise ValueError(f"portal sections must be ordered exactly as {expected}")
        return values
