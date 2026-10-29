"""Strict adapter for deterministic, provenance-rich canonical source snapshots."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from packages.domain.schemas import (
    DomainSchema,
    NormalizedAsset,
    NormalizedRecord,
    RelationCandidate,
    RepresentationCandidate,
    SourceConfig,
)

from .base import BaseAdapter, stable_uuid7
from .types import AdapterError, AdapterErrorCode, ScannedRecord

CANONICAL_SNAPSHOT_SCHEMA_VERSION = "research-asset-hub.canonical-source-snapshot/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _relative_snapshot_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith(("/", "//"))
        or _DRIVE_RE.match(normalized)
    ):
        raise ValueError("snapshot paths must be non-empty relative paths")
    path = PurePosixPath(normalized)
    if any(component in {"", ".", ".."} for component in path.parts):
        raise ValueError("snapshot paths cannot contain '.' or '..' segments")
    return path.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CanonicalSnapshotInput(DomainSchema):
    """One explicitly declared catalog/manifest input protected by a digest."""

    path: str
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, value: str) -> str:
        return _relative_snapshot_path(value)

    @field_validator("sha256")
    @classmethod
    def _sha_is_valid(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized


class CanonicalFieldPointer(DomainSchema):
    """A normalized field's exact upstream JSON Pointer and input digest."""

    json_pointer: str
    input_path: str | None = None
    input_sha256: str | None = None

    @field_validator("json_pointer")
    @classmethod
    def _pointer_is_valid(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("json_pointer must be an absolute JSON Pointer")
        return normalized

    @field_validator("input_path")
    @classmethod
    def _input_path_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_snapshot_path(value)

    @field_validator("input_sha256")
    @classmethod
    def _input_sha_is_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("input_sha256 must be a SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def _input_locator_is_complete(self) -> CanonicalFieldPointer:
        if (self.input_path is None) != (self.input_sha256 is None):
            raise ValueError("input_path and input_sha256 must be supplied together")
        return self


class CanonicalSnapshotItem(DomainSchema):
    """One lossless normalized source record plus exact upstream payloads."""

    source_record_id: str = Field(min_length=1)
    asset: NormalizedAsset
    field_pointers: dict[str, CanonicalFieldPointer] = Field(default_factory=dict)
    representations: list[RepresentationCandidate] = Field(default_factory=list)
    relations: list[RelationCandidate] = Field(default_factory=list)
    source_payload: dict[str, JsonValue] = Field(default_factory=dict)


class CanonicalSnapshotDocument(DomainSchema):
    """Immutable sidecar contract consumed by :class:`CanonicalSnapshotAdapter`."""

    schema_version: Literal[
        "research-asset-hub.canonical-source-snapshot/v1"
    ] = "research-asset-hub.canonical-source-snapshot/v1"
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    profile: str = Field(min_length=1)
    domain_slugs: list[str] = Field(
        min_length=1,
        validation_alias=AliasChoices("domain_slugs", "domain_slug"),
    )
    inputs: list[CanonicalSnapshotInput]
    items: list[CanonicalSnapshotItem]

    @field_validator("domain_slugs", mode="before")
    @classmethod
    def _accept_singular_domain(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("domain_slugs")
    @classmethod
    def _domains_are_unique_slugs(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("domain_slugs must not contain duplicates")
        if any(not re.fullmatch(r"^[a-z0-9][a-z0-9_-]{0,62}$", value) for value in normalized):
            raise ValueError("domain_slugs must contain only valid slugs")
        return normalized

    @model_validator(mode="after")
    def _document_is_internally_consistent(self) -> CanonicalSnapshotDocument:
        input_digests = {item.path: item.sha256 for item in self.inputs}
        if len(input_digests) != len(self.inputs):
            raise ValueError("snapshot inputs must use unique paths")

        record_ids = [item.source_record_id for item in self.items]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("snapshot items must use unique source_record_id values")

        for item in self.items:
            expected_asset_id = stable_uuid7(self.source_id, item.source_record_id)
            if item.asset.asset_id != expected_asset_id:
                raise ValueError(
                    f"asset_id for {item.source_record_id} is not the deterministic source id"
                )
            item_domains = set(item.asset.domain_slugs)
            if not item_domains:
                raise ValueError(
                    f"asset {item.source_record_id} must include at least one domain"
                )
            undeclared_domains = item_domains - set(self.domain_slugs)
            if undeclared_domains:
                raise ValueError(
                    f"asset {item.source_record_id} uses undeclared domains: "
                    f"{sorted(undeclared_domains)}"
                )
            for field_name, pointer in item.field_pointers.items():
                if field_name not in type(item.asset).model_fields:
                    raise ValueError(f"field pointer names unknown asset field: {field_name}")
                if pointer.input_path is not None:
                    expected_digest = input_digests.get(pointer.input_path)
                    if expected_digest is None:
                        raise ValueError(
                            f"field pointer references undeclared input: {pointer.input_path}"
                        )
                    if expected_digest != pointer.input_sha256:
                        raise ValueError(
                            f"field pointer digest differs from input: {pointer.input_path}"
                        )
            for representation in item.representations:
                if representation.source_id != self.source_id:
                    raise ValueError(
                        "every canonical representation must name the snapshot source_id"
                    )
                if any(mount.source_id != self.source_id for mount in representation.mounts):
                    raise ValueError(
                        "canonical representation mounts must name the snapshot source_id"
                    )
        return self


def canonical_snapshot_json_schema() -> dict[str, Any]:
    """Return the standalone JSON Schema 2020-12 contract."""

    schema = CanonicalSnapshotDocument.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = (
        "https://research-asset-hub.local/schemas/canonical-source-snapshot.schema.json"
    )
    return schema


class CanonicalSnapshotAdapter(BaseAdapter):
    """Read only a pre-built canonical snapshot; never infer files at scan time."""

    adapter_name = "canonical_snapshot"
    adapter_version = "1"
    default_record_selectors = ("items",)

    def __init__(
        self,
        config: SourceConfig | Mapping[str, Any],
        *,
        base_dir: Path | str | None = None,
    ) -> None:
        super().__init__(config, base_dir=base_dir)
        self._snapshot: CanonicalSnapshotDocument | None = None

    @property
    def manifest_source_path(self) -> str:
        """Return a stable locator for the one controlled out-of-root sidecar.

        Only canonical snapshots may live outside the immutable source root,
        and only below ``config/intake/<source-id>`` in this project.  The
        representation roots remain anchored to the real external source.
        """

        intake_root = (
            PROJECT_ROOT / "config" / "intake" / self.config.source_id
        ).resolve()
        manifest = self.manifest_absolute_path.resolve()
        try:
            relative = manifest.relative_to(intake_root)
        except ValueError as exc:
            raise AdapterError(
                AdapterErrorCode.PATH_OUTSIDE_ROOT,
                (
                    "Canonical snapshot manifest must be below "
                    f"config/intake/{self.config.source_id}"
                ),
                path=manifest,
                details={"allowed_root": str(intake_root)},
            ) from exc
        return (
            PurePosixPath("config", "intake", self.config.source_id, *relative.parts)
            .as_posix()
        )

    def scan(self, *, refresh: bool = False) -> list[ScannedRecord]:
        if self._scan_cache is not None and not refresh:
            return list(self._scan_cache)
        raw_document = self._read_json(
            self.manifest_absolute_path,
            missing_code=AdapterErrorCode.MISSING_MANIFEST,
        )
        try:
            snapshot = CanonicalSnapshotDocument.model_validate(raw_document)
        except ValidationError as exc:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "Canonical snapshot failed its strict schema",
                path=self.manifest_absolute_path,
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        if snapshot.source_id != self.config.source_id:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "Canonical snapshot source_id differs from SourceConfig",
                path=self.manifest_absolute_path,
                details={
                    "snapshot_source_id": snapshot.source_id,
                    "configured_source_id": self.config.source_id,
                },
            )
        configured_domains = set(self.config.domain_slugs)
        snapshot_domains = set(snapshot.domain_slugs)
        if configured_domains != snapshot_domains:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "Canonical snapshot domains differ from SourceConfig",
                path=self.manifest_absolute_path,
                details={
                    "configured_domains": sorted(configured_domains),
                    "snapshot_domains": sorted(snapshot_domains),
                },
            )
        for declared_input in snapshot.inputs:
            input_path = (self.source_root / declared_input.path).resolve()
            try:
                input_path.relative_to(self.source_root)
            except ValueError as exc:
                raise AdapterError(
                    AdapterErrorCode.PATH_OUTSIDE_ROOT,
                    "Canonical snapshot input escapes the read-only source root",
                    path=input_path,
                    details={"input_path": declared_input.path},
                ) from exc
            if not input_path.is_file():
                raise AdapterError(
                    AdapterErrorCode.MISSING_AUXILIARY,
                    "Canonical snapshot input no longer exists",
                    path=input_path,
                    details={"input_path": declared_input.path},
                )
            stat = input_path.stat()
            actual_sha256 = _sha256_file(input_path)
            if (
                stat.st_size != declared_input.size_bytes
                or actual_sha256 != declared_input.sha256
            ):
                raise AdapterError(
                    AdapterErrorCode.INVALID_RECORD,
                    "Canonical snapshot input differs from its frozen digest",
                    path=input_path,
                    details={
                        "input_path": declared_input.path,
                        "expected_size_bytes": declared_input.size_bytes,
                        "actual_size_bytes": stat.st_size,
                        "expected_sha256": declared_input.sha256,
                        "actual_sha256": actual_sha256,
                    },
                )
        scanned = [
            ScannedRecord(
                index=index,
                raw=item.model_dump(mode="json", by_alias=False),
                source_path=self.manifest_source_path,
            )
            for index, item in enumerate(snapshot.items)
        ]
        self._snapshot = snapshot
        self._scan_cache = scanned
        return list(scanned)

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        item = CanonicalSnapshotItem.model_validate(record.raw)
        snapshot = self._snapshot
        if snapshot is None:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "scan() must validate the canonical snapshot before normalize()",
                path=self.manifest_absolute_path,
            )
        return self.record(
            record,
            source_record_id=item.source_record_id,
            asset=item.asset,
            representations=item.representations,
            relations=item.relations,
            source_fields={
                "field_pointers": {
                    name: pointer.model_dump(mode="json")
                    for name, pointer in item.field_pointers.items()
                },
                "snapshot_schema_version": snapshot.schema_version,
                "snapshot_profile": snapshot.profile,
                "snapshot_inputs": [
                    source_input.model_dump(mode="json")
                    for source_input in snapshot.inputs
                ],
            },
        )
