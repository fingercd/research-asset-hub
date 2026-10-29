"""Closed vocabularies used by the operational data model.

Asset and domain names are intentionally *not* closed at the schema boundary:
new plugins may add namespaced asset types and arbitrary domain slugs without
requiring a database migration.  The enums below provide the well-known core
values and keep state machines and relation semantics strict.
"""

from enum import StrEnum


class AssetType(StrEnum):
    PAPER = "paper"
    REPOSITORY = "repository"
    DATASET = "dataset"
    METHOD = "method"
    BENCHMARK = "benchmark"
    HTML_DOCUMENT = "html_document"
    NOTE = "note"
    COLLECTION = "collection"


class Visibility(StrEnum):
    PRIVATE = "private"
    INTERNAL = "internal"
    PUBLIC = "public"


class RepresentationType(StrEnum):
    PDF = "pdf"
    HTML = "html"
    REPOSITORY = "repository"
    SOURCE_CODE = "source_code"
    ANNOTATION = "annotation"
    GUIDE = "guide"
    ANALYSIS = "analysis"
    MARKDOWN = "markdown"
    NOTEBOOK = "notebook"
    DATASET = "dataset"
    DATA = "data"
    IMAGE = "image"
    EXTRACTED_TEXT = "extracted_text"
    THUMBNAIL = "thumbnail"
    EXTERNAL_URL = "external_url"


class RepresentationRole(StrEnum):
    PRIMARY = "primary"
    ORIGINAL = "original"
    OFFICIAL = "official"
    MIRROR = "mirror"
    ANALYSIS = "analysis"
    GUIDE = "guide"
    ANNOTATED = "annotated"
    SUPPLEMENTAL = "supplemental"
    SNAPSHOT = "snapshot"
    DERIVED = "derived"


class RelationType(StrEnum):
    CITES = "cites"
    CITED_BY = "cited_by"
    EXTENDS = "extends"
    EXTENDED_BY = "extended_by"
    PRECEDES = "precedes"
    FOLLOWS = "follows"
    RELATED_TO = "related_to"
    IMPLEMENTS = "implements"
    IMPLEMENTED_BY = "implemented_by"
    HAS_REPRESENTATION = "has_representation"
    USES_METHOD = "uses_method"
    METHOD_USED_BY = "method_used_by"
    USES_DATASET = "uses_dataset"
    DATASET_USED_BY = "dataset_used_by"
    SAME_AS = "same_as"
    IS_VERSION_OF = "is_version_of"
    HAS_VERSION = "has_version"
    DERIVED_FROM = "derived_from"
    GENERATED = "generated"
    BELONGS_TO_COLLECTION = "belongs_to_collection"
    SIMILAR_TO = "similar_to"


class ReadingStatus(StrEnum):
    UNREAD = "unread"
    READING = "reading"
    READ = "read"


class AnnotationKind(StrEnum):
    GENERAL = "general"
    COMMENT = "comment"
    SUMMARY = "summary"
    QUESTION = "question"
    HIGHLIGHT = "highlight"


class WarningSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ConfirmationStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ImportOutcome(StrEnum):
    READY = "ready"
    SKIPPED = "skipped"
    ERROR = "error"


class ImportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"
    IMPORT = "import"
    MERGE = "merge"


class ProfileStatus(StrEnum):
    UNKNOWN = "unknown"
    DRAFT = "draft"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


class SourceAssetRole(StrEnum):
    PRIMARY = "primary"
    REPOSITORY = "repository"
    DATASET = "dataset"
    SUPPLEMENTAL = "supplemental"
    MENTIONED = "mentioned"


class FieldClaimStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class FieldResolutionKind(StrEnum):
    CLAIM = "claim"
    OVERRIDE = "override"
    CLEARED = "cleared"


class IdentityCandidateStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    MERGED = "merged"


class MaterializationKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    URL = "url"
    BUNDLE = "bundle"


class RepresentationAvailability(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"
    BLOCKED = "blocked"


class AnchorKind(StrEnum):
    PDF_PAGE = "pdf_page"
    HTML_HEADING = "html_heading"
    TEXT_LINES = "text_lines"
    CODE_LINES = "code_lines"
    NOTEBOOK_CELL = "notebook_cell"


class AnchorStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    NEEDS_REVIEW = "needs_review"
    ORPHANED = "orphaned"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


class ActivityStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class IndexKind(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    CODE = "code"


class IndexStatus(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"


class UsageKind(StrEnum):
    EMBEDDING = "embedding"
    CHAT = "chat"
    RERANK = "rerank"
    OCR = "ocr"


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SavedViewKind(StrEnum):
    SEARCH = "search"
    COMPARISON = "comparison"
    GRAPH = "graph"
    DOMAIN = "domain"


class IntegrationStatus(StrEnum):
    DISABLED = "disabled"
    READY = "ready"
    SYNCING = "syncing"
    ERROR = "error"


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


class SyncConflictStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class ConflictResolution(StrEnum):
    HUB = "hub"
    EXTERNAL = "external"
    BASE = "base"
    MANUAL = "manual"
    DELETE = "delete"


class TombstoneStatus(StrEnum):
    ACTIVE = "active"
    RESTORED = "restored"
    ACKNOWLEDGED = "acknowledged"


class BackupStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VERIFIED = "verified"
