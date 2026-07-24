"""Pydantic v2 schemas shared by source adapters, API and workers."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from .enums import (
    AnchorStatus,
    AssetType,
    BackupStatus,
    ChatRole,
    ConflictResolution,
    FieldClaimStatus,
    FieldResolutionKind,
    IdentityCandidateStatus,
    ImportOutcome,
    IndexKind,
    IndexStatus,
    IntegrationStatus,
    JobStatus,
    MaterializationKind,
    MessageStatus,
    RelationType,
    RepresentationAvailability,
    RepresentationRole,
    RepresentationType,
    SavedViewKind,
    SourceAssetRole,
    SyncConflictStatus,
    TombstoneStatus,
    UsageKind,
    Visibility,
    WarningSeverity,
)
from .identifiers import UUID7, new_uuid7

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CORE_ASSET_TYPES = {item.value for item in AssetType}
_EXTENSION_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[a-z][a-z0-9_-]{0,31}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DomainSchema(BaseModel):
    """Strict base class for persisted and adapter-facing contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


def _validate_slug(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    if not _SLUG_RE.fullmatch(normalized):
        raise ValueError(
            f"{label} must start with a lowercase letter or digit and contain "
            "only lowercase letters, digits, '_' or '-'"
        )
    return normalized


def _validate_relative_asset_path(value: str) -> str:
    """Normalize a representation path while preventing root escape.

    A representation is addressed by ``source_id + root_key + path_ref``.
    Parent segments are therefore never needed here: controlled sibling roots
    are declared once in ``SourceConfig.asset_roots`` and selected by root_key.
    """

    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("path_ref cannot be empty")
    if "\x00" in normalized:
        raise ValueError("path_ref cannot contain NUL")
    if normalized.startswith(("/", "//")) or _DRIVE_RE.match(normalized):
        raise ValueError("path_ref must be relative to a declared source root")

    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path_ref cannot contain '.' or '..' segments")
    return path.as_posix()


def _validate_declared_config_path(value: str) -> str:
    """Validate an explicitly configured path or glob.

    Source roots may intentionally contain ``..`` (for example the existing
    reinforcement-learning PDF library).  These paths are trusted only because
    they are explicitly declared in configuration; discovered records never
    receive this relaxed type.
    """

    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("configured path cannot be empty")
    if "\x00" in normalized:
        raise ValueError("configured path cannot contain NUL")
    return normalized


def _canonical_external_identifier(scheme: str, value: str) -> str:
    normalized = value.strip()
    lowered_scheme = scheme.lower()
    if lowered_scheme == "doi":
        normalized = re.sub(
            r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).lower()
        if not normalized.startswith("10.") or "/" not in normalized:
            raise ValueError("DOI must look like '10.<registrant>/<suffix>'")
    elif lowered_scheme == "arxiv":
        normalized = re.sub(
            r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\.pdf$", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"v\d+$", "", normalized, flags=re.IGNORECASE).lower()
    elif lowered_scheme in {"github", "url"}:
        normalized = normalized.rstrip("/")
    elif lowered_scheme in {"zotero", "isbn", "source"}:
        normalized = normalized.strip()
    if not normalized:
        raise ValueError("external identifier value cannot be empty")
    return normalized


class SourceConfig(DomainSchema):
    """One declared, read-only source and its adapter configuration.

    Aliases match the human-maintained ``config/sources.yaml`` while canonical
    serialization uses explicit field names.
    """

    source_id: str = Field(validation_alias=AliasChoices("source_id", "id", "alias"))
    label: NonEmptyString
    adapter: NonEmptyString
    root_path: str = Field(validation_alias=AliasChoices("root_path", "root"))
    manifest_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("manifest_path", "manifest"),
    )
    read_only: bool = True
    enabled: bool = True
    domain_slugs: list[str] = Field(default_factory=list)
    expected_records: int | None = Field(default=None, ge=0)
    auxiliary: dict[str, str] = Field(default_factory=dict)
    asset_roots: dict[str, str] = Field(default_factory=dict)
    formal_html: list[str] = Field(default_factory=list)
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_singular_domain(cls, value: Any) -> Any:
        if isinstance(value, dict) and "domain_slugs" not in value:
            copied = dict(value)
            domains = copied.pop("domains", None)
            domain = copied.pop("domain", None)
            if domains is not None:
                copied["domain_slugs"] = domains
            elif domain is not None:
                copied["domain_slugs"] = [domain]
            return copied
        return value

    @field_validator("source_id")
    @classmethod
    def _source_id_is_slug(cls, value: str) -> str:
        return _validate_slug(value, label="source_id")

    @field_validator("domain_slugs")
    @classmethod
    def _domains_are_unique_slugs(cls, values: list[str]) -> list[str]:
        normalized = [_validate_slug(item, label="domain slug") for item in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("domain_slugs must not contain duplicates")
        return normalized

    @field_validator("root_path", "manifest_path")
    @classmethod
    def _configured_path(cls, value: str | None) -> str | None:
        return None if value is None else _validate_declared_config_path(value)

    @field_validator("auxiliary", "asset_roots")
    @classmethod
    def _configured_path_map(cls, values: dict[str, str]) -> dict[str, str]:
        return {
            _validate_slug(key, label="declared root key"): _validate_declared_config_path(path)
            for key, path in values.items()
        }

    @field_validator("formal_html")
    @classmethod
    def _configured_globs(cls, values: list[str]) -> list[str]:
        return [_validate_declared_config_path(value) for value in values]


class ExternalIdentifier(DomainSchema):
    """An identifier from an external authority with a canonical match key."""

    scheme: str
    scope: str = Field(default="", max_length=255)
    value: NonEmptyString
    canonical: str | None = None
    is_identity: bool | None = None

    @field_validator("scheme")
    @classmethod
    def _scheme_is_valid(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SCHEME_RE.fullmatch(normalized):
            raise ValueError("scheme must be a lowercase identifier")
        return normalized

    @model_validator(mode="after")
    def _derive_canonical_value(self) -> ExternalIdentifier:
        expected = _canonical_external_identifier(self.scheme, self.value)
        if self.canonical is not None and self.canonical != expected:
            raise ValueError("canonical does not match the normalized identifier value")
        object.__setattr__(self, "canonical", expected)
        if self.is_identity is None:
            object.__setattr__(
                self,
                "is_identity",
                self.scheme in {"doi", "arxiv", "github", "zotero", "isbn", "source"},
            )
        return self


class AssetReference(DomainSchema):
    """Reference a relation endpoint without inventing a local asset UUID.

    Exactly one selector is required.  A missing ``source_id`` on a
    ``source_record_id`` is filled from the containing ``NormalizedRecord``.
    """

    asset_id: UUID7 | None = None
    source_id: str | None = None
    source_record_id: str | None = None
    external_identifier: ExternalIdentifier | None = None

    @field_validator("source_id")
    @classmethod
    def _optional_source_id_is_slug(cls, value: str | None) -> str | None:
        return None if value is None else _validate_slug(value, label="source_id")

    @model_validator(mode="after")
    def _one_selector_only(self) -> AssetReference:
        selectors = (
            self.asset_id is not None,
            self.source_record_id is not None,
            self.external_identifier is not None,
        )
        if sum(selectors) != 1:
            raise ValueError(
                "asset reference requires exactly one of asset_id, "
                "source_record_id or external_identifier"
            )
        if self.source_id is not None and self.source_record_id is None:
            raise ValueError("source_id is only valid with source_record_id")
        return self


class NormalizedAsset(DomainSchema):
    """Adapter-neutral asset metadata.

    Known core asset types use :class:`AssetType`; plugins can add a type as a
    namespaced value such as ``my_plugin:simulation``.  Domain slugs are open
    and therefore adding a seventh domain never requires a code change.
    """

    asset_id: UUID7 = Field(default_factory=new_uuid7)
    asset_type: str = AssetType.PAPER.value
    title: NonEmptyString
    title_zh: str | None = None
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1450, le=2200)
    venue: str | None = None
    language: str | None = None
    abstract: str | None = None
    summary: str | None = None
    content: str | None = None
    analysis: dict[str, JsonValue] = Field(default_factory=dict)
    visibility: Visibility = Visibility.PRIVATE
    external_identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    source_urls: list[AnyUrl] = Field(default_factory=list)
    domain_slugs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)

    @field_validator("asset_type", mode="before")
    @classmethod
    def _asset_type_is_core_or_namespaced(cls, value: Any) -> str:
        normalized = value.value if isinstance(value, AssetType) else str(value).strip().lower()
        if normalized not in _CORE_ASSET_TYPES and not _EXTENSION_NAME_RE.fullmatch(normalized):
            raise ValueError(
                "asset_type must be a core type or a namespaced extension such as 'plugin:type'"
            )
        return normalized

    @field_validator("domain_slugs")
    @classmethod
    def _domain_slugs_are_valid(cls, values: list[str]) -> list[str]:
        normalized = [_validate_slug(item, label="domain slug") for item in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("domain_slugs must not contain duplicates")
        return normalized

    @field_validator("authors", "tags")
    @classmethod
    def _string_lists_are_clean(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values if item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("list values must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def _external_identifiers_are_unique(self) -> NormalizedAsset:
        keys = [
            (item.scheme, item.scope, item.canonical)
            for item in self.external_identifiers
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("external_identifiers must not contain duplicates")
        return self


class RepresentationCandidate(DomainSchema):
    """A file, directory or URL representation discovered by an adapter."""

    representation_type: RepresentationType
    role: RepresentationRole = RepresentationRole.ORIGINAL
    materialization_kind: MaterializationKind = MaterializationKind.FILE
    availability: RepresentationAvailability = RepresentationAvailability.UNKNOWN
    source_id: str | None = None
    root_key: str = "source"
    path_ref: str | None = None
    url: AnyUrl | None = None
    entrypoint: str | None = None
    revision: str | None = None
    tree_digest: str | None = None
    sha256: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    network_required: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    verified_at: datetime | None = None

    @field_validator("source_id")
    @classmethod
    def _source_is_slug(cls, value: str | None) -> str | None:
        return None if value is None else _validate_slug(value, label="source_id")

    @field_validator("root_key")
    @classmethod
    def _root_key_is_slug(cls, value: str) -> str:
        return _validate_slug(value, label="root_key")

    @field_validator("path_ref", "entrypoint")
    @classmethod
    def _path_is_bounded(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_asset_path(value)

    @field_validator("sha256", "tree_digest")
    @classmethod
    def _sha256_is_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def _has_location(self) -> RepresentationCandidate:
        if self.path_ref is None and self.url is None:
            raise ValueError("representation requires path_ref or url")
        return self


class RelationEvidence(DomainSchema):
    source: NonEmptyString
    path: str | None = None
    field: str | None = None
    excerpt: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class RelationCandidate(DomainSchema):
    """A typed, evidenced edge proposed by an adapter."""

    relation_type: RelationType
    object: AssetReference = Field(validation_alias=AliasChoices("object", "object_ref", "target"))
    subject: AssetReference | None = Field(
        default=None,
        validation_alias=AliasChoices("subject", "subject_ref"),
    )
    evidence: RelationEvidence
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confirmed: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AdapterWarning(DomainSchema):
    code: NonEmptyString
    message: NonEmptyString
    severity: WarningSeverity = WarningSeverity.WARNING
    field: str | None = None
    path: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)
    recoverable: bool = True


class NormalizedRecord(DomainSchema):
    """The complete, serializable result for one source manifest record."""

    schema_version: int = Field(default=1, ge=1)
    source_id: str
    source_record_id: NonEmptyString
    source_path: str | None = None
    adapter_name: NonEmptyString
    adapter_version: NonEmptyString = "1"
    outcome: ImportOutcome = ImportOutcome.READY
    asset: NormalizedAsset
    representations: list[RepresentationCandidate] = Field(default_factory=list)
    relations: list[RelationCandidate] = Field(default_factory=list)
    warnings: list[AdapterWarning] = Field(default_factory=list)
    source_fields: dict[str, JsonValue] = Field(default_factory=dict)
    raw: dict[str, JsonValue] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("raw", "raw_record"),
    )
    normalized_at: datetime | None = None

    @field_validator("source_id")
    @classmethod
    def _source_id_is_slug(cls, value: str) -> str:
        return _validate_slug(value, label="source_id")

    @field_validator("source_path")
    @classmethod
    def _source_path_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_asset_path(value)

    @model_validator(mode="after")
    def _inherit_source_context(self) -> NormalizedRecord:
        for representation in self.representations:
            if representation.source_id is None:
                representation.source_id = self.source_id
        for relation in self.relations:
            for reference in (relation.subject, relation.object):
                if (
                    reference is not None
                    and reference.source_record_id is not None
                    and reference.source_id is None
                ):
                    reference.source_id = self.source_id
        return self


class PaperProfileData(DomainSchema):
    publication_status: str = "unknown"
    paper_type: str | None = None
    research_question: str | None = None
    mechanism: str | None = None
    contributions: list[str] = Field(default_factory=list)
    evidence_summary: str | None = None
    limitations: list[str] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class RepositoryProfileData(DomainSchema):
    canonical_url: AnyUrl | None = None
    host: str | None = None
    owner: str | None = None
    name: str | None = None
    default_branch: str | None = None
    pinned_revision: str | None = None
    license_spdx: str | None = None
    code_status: str = "unknown"
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_repository_identity(self) -> RepositoryProfileData:
        if self.canonical_url is None and not (self.host and self.owner and self.name):
            raise ValueError(
                "repository profile requires canonical_url or host/owner/name"
            )
        return self


class MaterializedAssetCandidate(DomainSchema):
    """A non-primary asset derived while resolving one normalized source record."""

    source_role: SourceAssetRole
    asset: NormalizedAsset
    paper_profile: PaperProfileData | None = None
    repository_profile: RepositoryProfileData | None = None
    representations: list[RepresentationCandidate] = Field(default_factory=list)
    relations: list[RelationCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _profile_matches_asset_type(self) -> MaterializedAssetCandidate:
        if self.asset.asset_type == AssetType.PAPER.value and self.repository_profile:
            raise ValueError("paper assets cannot carry repository_profile")
        if (
            self.asset.asset_type == AssetType.REPOSITORY.value
            and self.repository_profile is None
        ):
            raise ValueError("repository assets require repository_profile")
        return self


class NormalizedMaterializationBatch(DomainSchema):
    """One source record plus independently materialized shared assets."""

    record: NormalizedRecord
    related_assets: list[MaterializedAssetCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _asset_ids_are_unique(self) -> NormalizedMaterializationBatch:
        asset_ids = [
            self.record.asset.asset_id,
            *(candidate.asset.asset_id for candidate in self.related_assets),
        ]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("materialization batch contains duplicate asset_id values")
        return self


class SourceRecordAssetCandidate(DomainSchema):
    asset_id: UUID7
    role: SourceAssetRole = SourceAssetRole.PRIMARY
    source_record_version_id: UUID7 | None = None
    evidence: dict[str, JsonValue] = Field(default_factory=dict)


class FieldClaimCandidate(DomainSchema):
    entity_type: NonEmptyString
    entity_id: UUID7
    field_path: str
    value: JsonValue
    source_record_version_id: UUID7 | None = None
    provider: str | None = None
    source_pointer: str | None = None
    authority: int = Field(default=0, ge=-1000, le=1000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: FieldClaimStatus = FieldClaimStatus.CANDIDATE

    @field_validator("field_path")
    @classmethod
    def _field_path_is_json_pointer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/") or normalized == "/":
            raise ValueError("field_path must be a non-root JSON Pointer")
        return normalized


class FieldResolutionRequest(DomainSchema):
    entity_type: NonEmptyString
    entity_id: UUID7
    field_path: str
    resolution_kind: FieldResolutionKind
    selected_claim_id: UUID7 | None = None
    override_value: JsonValue | None = None
    reason: str | None = None

    @field_validator("field_path")
    @classmethod
    def _resolution_path_is_json_pointer(cls, value: str) -> str:
        return FieldClaimCandidate._field_path_is_json_pointer(value)

    @model_validator(mode="after")
    def _resolution_payload_matches_kind(self) -> FieldResolutionRequest:
        if (
            self.resolution_kind == FieldResolutionKind.CLAIM
            and self.selected_claim_id is None
        ):
            raise ValueError("claim resolution requires selected_claim_id")
        if (
            self.resolution_kind != FieldResolutionKind.CLAIM
            and self.selected_claim_id is not None
        ):
            raise ValueError("selected_claim_id is only valid for claim resolution")
        return self


class IdentityCandidatePayload(DomainSchema):
    left_asset_id: UUID7
    right_asset_id: UUID7
    reason: NonEmptyString
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    status: IdentityCandidateStatus = IdentityCandidateStatus.PENDING
    evidence: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _assets_are_distinct(self) -> IdentityCandidatePayload:
        if self.left_asset_id == self.right_asset_id:
            raise ValueError("identity candidate assets must be distinct")
        return self


class RepresentationMountCandidate(DomainSchema):
    mount_path: str = "/"
    source_id: str
    root_key: str = "source"
    path_ref: str
    read_only: bool = True
    priority: int = 0

    @field_validator("source_id", "root_key")
    @classmethod
    def _mount_slugs_are_valid(cls, value: str, info: Any) -> str:
        return _validate_slug(value, label=info.field_name)

    @field_validator("mount_path")
    @classmethod
    def _mount_path_is_bounded(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if normalized == "/":
            return normalized
        return "/" + _validate_relative_asset_path(normalized.lstrip("/"))

    @field_validator("path_ref")
    @classmethod
    def _mount_source_path_is_bounded(cls, value: str) -> str:
        return _validate_relative_asset_path(value)


class AnchorBase(DomainSchema):
    representation_digest: str | None = None
    quote_hash: str | None = None
    status: AnchorStatus = AnchorStatus.CURRENT

    @field_validator("representation_digest", "quote_hash")
    @classmethod
    def _anchor_digest_is_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("anchor digest must be SHA-256")
        return normalized


class PdfPageAnchor(AnchorBase):
    kind: Literal["pdf_page"] = "pdf_page"
    page_number: int = Field(ge=1)
    bounding_box: tuple[float, float, float, float] | None = None


class HtmlHeadingAnchor(AnchorBase):
    kind: Literal["html_heading"] = "html_heading"
    heading_id: str | None = None
    heading_path: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_heading_locator(self) -> HtmlHeadingAnchor:
        if not self.heading_id and not self.heading_path:
            raise ValueError("HTML anchor requires heading_id or heading_path")
        return self


class LineRangeAnchor(AnchorBase):
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _line_range_is_ordered(self) -> LineRangeAnchor:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class TextLineAnchor(LineRangeAnchor):
    kind: Literal["text_lines"] = "text_lines"


class CodeLineAnchor(LineRangeAnchor):
    kind: Literal["code_lines"] = "code_lines"
    path_ref: str
    symbol: str | None = None

    @field_validator("path_ref")
    @classmethod
    def _code_path_is_bounded(cls, value: str) -> str:
        return _validate_relative_asset_path(value)


class NotebookCellAnchor(AnchorBase):
    kind: Literal["notebook_cell"] = "notebook_cell"
    cell_id: str | None = None
    cell_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _has_cell_locator(self) -> NotebookCellAnchor:
        if self.cell_id is None and self.cell_index is None:
            raise ValueError("notebook anchor requires cell_id or cell_index")
        return self


AnnotationAnchor = Annotated[
    PdfPageAnchor
    | HtmlHeadingAnchor
    | TextLineAnchor
    | CodeLineAnchor
    | NotebookCellAnchor,
    Field(discriminator="kind"),
]


class AnnotationRevisionData(DomainSchema):
    revision: int = Field(ge=1)
    title: str | None = None
    summary: str | None = None
    body_markdown: str
    anchor: AnnotationAnchor | None = None
    editor: NonEmptyString = "local"
    source: NonEmptyString = "hub"


class JobRequest(DomainSchema):
    job_type: NonEmptyString
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-1000, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=100)
    dedupe_key: str | None = None


class JobStateData(DomainSchema):
    job_id: UUID7
    status: JobStatus
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    attempts: int = Field(default=0, ge=0)
    heartbeat_at: datetime | None = None
    error: dict[str, JsonValue] | None = None


class DocumentChunkData(DomainSchema):
    asset_id: UUID7
    representation_id: UUID7
    chunk_index: int = Field(ge=0)
    chunk_kind: NonEmptyString
    text: str
    content_hash: str
    parser_name: NonEmptyString
    parser_version: NonEmptyString
    anchor: AnnotationAnchor | None = None
    language: str | None = None
    token_count: int | None = Field(default=None, ge=0)

    @field_validator("content_hash")
    @classmethod
    def _chunk_hash_is_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("content_hash must be SHA-256")
        return normalized


class IndexStateData(DomainSchema):
    index_name: NonEmptyString
    index_kind: IndexKind
    entity_type: NonEmptyString
    entity_id: UUID7
    content_digest: str
    status: IndexStatus = IndexStatus.PENDING
    provider: str | None = None
    model: str | None = None
    dimension: int | None = Field(default=None, ge=1)
    version: NonEmptyString = "1"

    @field_validator("content_digest")
    @classmethod
    def _index_digest_is_sha256(cls, value: str) -> str:
        return DocumentChunkData._chunk_hash_is_sha256(value)


class ProviderUsageData(DomainSchema):
    provider: NonEmptyString
    usage_kind: UsageKind
    model: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    document_count: int = Field(default=0, ge=0)
    estimated_cost: Decimal | None = Field(default=None, ge=0)
    actual_cost: Decimal | None = Field(default=None, ge=0)
    currency: str = "USD"
    privacy_scope: str = "private"


class ChatCitationData(DomainSchema):
    asset_id: UUID7
    representation_id: UUID7
    chunk_id: UUID7
    ordinal: int = Field(ge=1)
    excerpt: str | None = None
    anchor: AnnotationAnchor | None = None
    content_digest: str
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("content_digest")
    @classmethod
    def _citation_digest_is_sha256(cls, value: str) -> str:
        return DocumentChunkData._chunk_hash_is_sha256(value)


class ChatMessageData(DomainSchema):
    role: ChatRole
    content: str
    sequence: int = Field(ge=0)
    status: MessageStatus = MessageStatus.COMPLETE
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    citations: list[ChatCitationData] = Field(default_factory=list)


class SavedViewData(DomainSchema):
    name: NonEmptyString
    view_kind: SavedViewKind
    definition: dict[str, JsonValue]
    visibility: Visibility = Visibility.PRIVATE


class IntegrationConfigData(DomainSchema):
    provider: NonEmptyString
    name: NonEmptyString
    config: dict[str, JsonValue] = Field(default_factory=dict)
    status: IntegrationStatus = IntegrationStatus.READY
    enabled: bool = True


class SyncSnapshotData(DomainSchema):
    external_object_id: UUID7
    base_document: dict[str, JsonValue]
    hub_revision: int | None = Field(default=None, ge=0)
    external_revision: str | None = None
    hub_hash: str | None = None
    external_hash: str | None = None

    @field_validator("hub_hash", "external_hash")
    @classmethod
    def _sync_hash_is_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return DocumentChunkData._chunk_hash_is_sha256(value)


class SyncConflictData(DomainSchema):
    external_object_id: UUID7
    entity_type: NonEmptyString
    entity_id: UUID7
    field_path: str
    base_value: JsonValue | None = None
    hub_value: JsonValue | None = None
    external_value: JsonValue | None = None
    status: SyncConflictStatus = SyncConflictStatus.PENDING
    resolution: ConflictResolution | None = None
    resolved_value: JsonValue | None = None

    @field_validator("field_path")
    @classmethod
    def _conflict_path_is_json_pointer(cls, value: str) -> str:
        return FieldClaimCandidate._field_path_is_json_pointer(value)


class TombstoneData(DomainSchema):
    integration_id: UUID7
    entity_type: NonEmptyString
    entity_id: UUID7 | None = None
    external_type: NonEmptyString
    external_id: NonEmptyString
    side: Literal["hub", "external"]
    status: TombstoneStatus = TombstoneStatus.ACTIVE
    reason: str | None = None


class BackupRunData(DomainSchema):
    status: BackupStatus = BackupStatus.PENDING
    backup_kind: NonEmptyString = "full"
    destination_label: NonEmptyString
    manifest_path: str | None = None
    database_digest: str | None = None
    export_digest: str | None = None
    local_copy_only: bool = True

    @field_validator("database_digest", "export_digest")
    @classmethod
    def _backup_digest_is_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return DocumentChunkData._chunk_hash_is_sha256(value)


def normalized_record_json_schema() -> dict[str, Any]:
    """Return the authoritative adapter contract as JSON Schema 2020-12."""

    schema = NormalizedRecord.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://research-asset-hub.local/schemas/normalized-record-v1.json"
    return schema
