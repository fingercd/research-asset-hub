"""Read-only source adapter contract and plugin registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlparse
from uuid import UUID

from pydantic import AnyUrl, ValidationError

from packages.domain.enums import (
    ImportOutcome,
    RelationType,
    RepresentationRole,
    RepresentationType,
    WarningSeverity,
)
from packages.domain.schemas import (
    AdapterWarning,
    AssetReference,
    ExternalIdentifier,
    NormalizedAsset,
    NormalizedRecord,
    RelationCandidate,
    RelationEvidence,
    RepresentationCandidate,
    SourceConfig,
)

from .types import AdapterError, AdapterErrorCode, AdapterPreview, ScannedRecord

_T = TypeVar("_T", bound="BaseAdapter")
_ARXIV_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"(?:doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)


def get_in(value: Any, dotted_path: str | None, default: Any = None) -> Any:
    """Read a dotted mapping/list path without evaluating expressions."""

    if dotted_path is None or dotted_path == "":
        return value
    current = value
    for component in dotted_path.split("."):
        if isinstance(current, Mapping):
            if component not in current:
                return default
            current = current[component]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            try:
                current = current[int(component)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def string_list(value: Any, *, separators: tuple[str, ...] = (";",)) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        pieces = [value]
        for separator in separators:
            pieces = [part for piece in pieces for part in piece.split(separator)]
    elif isinstance(value, Sequence):
        pieces = [str(item) for item in value if item is not None]
    else:
        pieces = [str(value)]
    result: list[str] = []
    for piece in pieces:
        normalized = piece.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def http_urls(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = string_or_none(value)
        if text is None:
            continue
        parsed = urlparse(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        if text not in result:
            result.append(text)
    return result


def stable_uuid7(source_id: str, source_record_id: str) -> UUID:
    """Derive a repeatable UUIDv7-shaped candidate id from source identity.

    The importer may replace this candidate when it merges the same paper from
    multiple sources.  Keeping the adapter output deterministic makes dry-runs
    and fixture snapshots stable.
    """

    digest = hashlib.sha256(f"{source_id}\0{source_record_id}".encode()).digest()
    timestamp_ms = int.from_bytes(digest[:6], "big")
    random_a = int.from_bytes(digest[6:8], "big") & ((1 << 12) - 1)
    random_b = int.from_bytes(digest[8:16], "big") & ((1 << 62) - 1)
    integer = (timestamp_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return UUID(int=integer)


class BaseAdapter(ABC):
    """Base class for deterministic, read-only manifest adapters."""

    adapter_name: ClassVar[str] = "base"
    adapter_version: ClassVar[str] = "1"
    default_record_selectors: ClassVar[tuple[str, ...]] = ("papers", "items")

    def __init__(
        self,
        config: SourceConfig | Mapping[str, Any],
        *,
        base_dir: Path | str | None = None,
    ) -> None:
        self.config = (
            config if isinstance(config, SourceConfig) else SourceConfig.model_validate(config)
        )
        self.base_dir = Path(base_dir or Path.cwd()).resolve()
        configured_root = Path(self.config.root_path)
        self.source_root = (
            configured_root.resolve()
            if configured_root.is_absolute()
            else (self.base_dir / configured_root).resolve()
        )
        self._scan_cache: list[ScannedRecord] | None = None
        self._aux_cache: dict[str, Any] = {}

    @property
    def manifest_absolute_path(self) -> Path:
        if self.config.manifest_path is None:
            raise AdapterError(
                AdapterErrorCode.MISSING_MANIFEST,
                f"Source '{self.config.source_id}' has no manifest path",
            )
        configured = Path(self.config.manifest_path)
        return (
            configured.resolve()
            if configured.is_absolute()
            else (self.source_root / configured).resolve()
        )

    @property
    def manifest_source_path(self) -> str:
        try:
            return self.manifest_absolute_path.relative_to(self.source_root).as_posix()
        except ValueError as exc:
            raise AdapterError(
                AdapterErrorCode.PATH_OUTSIDE_ROOT,
                "Configured manifest is outside the declared source root",
                path=self.manifest_absolute_path,
            ) from exc

    def _read_json(self, path: Path, *, missing_code: AdapterErrorCode) -> Any:
        if not path.is_file():
            raise AdapterError(
                missing_code,
                f"JSON input does not exist: {path}",
                path=path,
            )
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdapterError(
                AdapterErrorCode.INVALID_JSON,
                f"Invalid UTF-8 JSON in {path}: {exc}",
                path=path,
                details={
                    "line": getattr(exc, "lineno", None),
                    "column": getattr(exc, "colno", None),
                },
            ) from exc

    def load_auxiliary(self, key: str, *, required: bool = False) -> Any:
        if key in self._aux_cache:
            return self._aux_cache[key]
        configured = self.config.auxiliary.get(key)
        if configured is None:
            if required:
                raise AdapterError(
                    AdapterErrorCode.MISSING_AUXILIARY,
                    f"Source '{self.config.source_id}' has no auxiliary '{key}'",
                )
            return None
        path_value = Path(configured)
        path = (
            path_value.resolve()
            if path_value.is_absolute()
            else (self.source_root / path_value).resolve()
        )
        value = self._read_json(path, missing_code=AdapterErrorCode.MISSING_AUXILIARY)
        self._aux_cache[key] = value
        return value

    def _select_records(self, document: Any, selector: str | None = None) -> list[Any]:
        if selector:
            selected = get_in(document, selector)
            if not isinstance(selected, list):
                raise AdapterError(
                    AdapterErrorCode.UNSUPPORTED_ROOT,
                    f"Record selector '{selector}' did not resolve to an array",
                    path=self.manifest_absolute_path,
                )
            return selected
        if isinstance(document, list):
            return document
        if isinstance(document, Mapping):
            for candidate in self.default_record_selectors:
                selected = document.get(candidate)
                if isinstance(selected, list):
                    return selected
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_ROOT,
            "Manifest root must be an array or contain a 'papers'/'items' array",
            path=self.manifest_absolute_path,
        )

    def scan(self, *, refresh: bool = False) -> list[ScannedRecord]:
        """Read the declared manifest without changing any source file."""

        if self._scan_cache is not None and not refresh:
            return list(self._scan_cache)
        document = self._read_json(
            self.manifest_absolute_path,
            missing_code=AdapterErrorCode.MISSING_MANIFEST,
        )
        selector = string_or_none(self.config.options.get("record_selector"))
        raw_records = self._select_records(document, selector)
        scanned: list[ScannedRecord] = []
        for index, raw in enumerate(raw_records):
            if not isinstance(raw, Mapping):
                raise AdapterError(
                    AdapterErrorCode.INVALID_RECORD,
                    f"Manifest record {index} is not an object",
                    path=self.manifest_absolute_path,
                    details={"index": index, "type": type(raw).__name__},
                )
            scanned.append(
                ScannedRecord(
                    index=index,
                    raw=dict(raw),
                    source_path=self.manifest_source_path,
                )
            )
        self._scan_cache = scanned
        return list(scanned)

    def prepare(self, records: Sequence[ScannedRecord]) -> None:
        """Hook for auxiliary joins and title/id indexes before normalization."""
        return None

    @abstractmethod
    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        """Map one scanned object to the shared domain contract."""

    def validate(self, record: NormalizedRecord | Mapping[str, Any]) -> NormalizedRecord:
        try:
            payload = (
                record.model_dump(mode="python")
                if isinstance(record, NormalizedRecord)
                else dict(record)
            )
            return NormalizedRecord.model_validate(payload)
        except ValidationError as exc:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "Normalized record failed the shared schema",
                path=self.manifest_absolute_path,
                details={"errors": exc.errors(include_url=False)},
            ) from exc

    def load(self, *, refresh: bool = False) -> list[NormalizedRecord]:
        scanned = self.scan(refresh=refresh)
        self.prepare(scanned)
        normalized: list[NormalizedRecord] = []
        seen: dict[str, int] = {}
        for source_record in scanned:
            record = self.validate(self.normalize(source_record))
            duplicate_index = seen.get(record.source_record_id)
            if duplicate_index is not None:
                raise AdapterError(
                    AdapterErrorCode.DUPLICATE_SOURCE_RECORD_ID,
                    (
                        f"Duplicate source_record_id '{record.source_record_id}' "
                        f"at records {duplicate_index} and {source_record.index}"
                    ),
                    path=self.manifest_absolute_path,
                    details={
                        "source_record_id": record.source_record_id,
                        "first_index": duplicate_index,
                        "duplicate_index": source_record.index,
                    },
                )
            seen[record.source_record_id] = source_record.index
            normalized.append(record)
        return normalized

    def preview(self, *, limit: int = 5, refresh: bool = False) -> AdapterPreview:
        records = self.load(refresh=refresh)
        outcomes = Counter(record.outcome.value for record in records)
        warnings = Counter(warning.code for record in records for warning in record.warnings)
        representations = Counter(
            representation.representation_type.value
            for record in records
            for representation in record.representations
        )
        relations = Counter(
            relation.relation_type.value for record in records for relation in record.relations
        )
        return AdapterPreview(
            source_id=self.config.source_id,
            adapter_name=self.adapter_name,
            total_records=len(records),
            outcomes=dict(sorted(outcomes.items())),
            warning_counts=dict(sorted(warnings.items())),
            representation_counts=dict(sorted(representations.items())),
            relation_counts=dict(sorted(relations.items())),
            sample_records=tuple(records[: max(0, limit)]),
        )

    def _declared_root(self, root_key: str) -> Path:
        if root_key == "source":
            return self.source_root
        configured = self.config.asset_roots.get(root_key)
        if configured is None:
            raise AdapterError(
                AdapterErrorCode.UNKNOWN_ROOT_KEY,
                f"Source '{self.config.source_id}' has no asset root '{root_key}'",
                details={"root_key": root_key},
            )
        value = Path(configured)
        return value.resolve() if value.is_absolute() else (self.source_root / value).resolve()

    @staticmethod
    def _relative_if_within(candidate: Path, root: Path) -> str | None:
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None

    def path_ref(self, value: str | Path, *, root_key: str = "source") -> str:
        """Convert a discovered path to a traversal-free declared-root reference."""

        raw = str(value).strip()
        if not raw or "\x00" in raw:
            raise AdapterError(
                AdapterErrorCode.PATH_OUTSIDE_ROOT,
                "Discovered asset path is empty or contains NUL",
                details={"value": raw, "root_key": root_key},
            )
        declared_root = self._declared_root(root_key)
        windows_path = PureWindowsPath(raw)
        host_root = os.environ.get("HUB_HOST_WORKSPACE_ROOT")
        container_root = os.environ.get("HUB_CONTAINER_WORKSPACE_ROOT")
        if windows_path.is_absolute() and host_root and container_root:
            try:
                windows_relative = windows_path.relative_to(PureWindowsPath(host_root))
            except ValueError as exc:
                raise AdapterError(
                    AdapterErrorCode.PATH_OUTSIDE_ROOT,
                    "Windows asset path is outside the mapped host workspace",
                    details={"value": raw, "root_key": root_key},
                ) from exc
            raw = str(Path(container_root, *windows_relative.parts))
        elif os.name != "nt" and windows_path.is_absolute():
            raise AdapterError(
                AdapterErrorCode.PATH_OUTSIDE_ROOT,
                "Windows asset path requires an explicit container path mapping",
                details={"value": raw, "root_key": root_key},
            )
        raw_path = Path(raw.replace("/", str(Path("/"))))
        candidates: list[Path] = []
        if raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            candidates.extend((self.source_root / raw_path, declared_root / raw_path))

        for candidate in candidates:
            candidate_relative = self._relative_if_within(candidate, declared_root)
            if candidate_relative and ".." not in PurePosixPath(candidate_relative).parts:
                return candidate_relative
        raise AdapterError(
            AdapterErrorCode.PATH_OUTSIDE_ROOT,
            f"Discovered path escapes declared root '{root_key}': {raw}",
            details={
                "value": raw,
                "root_key": root_key,
                "declared_root": str(declared_root),
            },
        )

    def path_exists(self, path_ref: str, *, root_key: str = "source") -> bool:
        return (self._declared_root(root_key) / Path(path_ref)).exists()

    def warning(
        self,
        code: str,
        message: str,
        *,
        severity: WarningSeverity = WarningSeverity.WARNING,
        field: str | None = None,
        path: str | None = None,
        details: Mapping[str, Any] | None = None,
        recoverable: bool = True,
    ) -> AdapterWarning:
        return AdapterWarning(
            code=code,
            message=message,
            severity=severity,
            field=field,
            path=path,
            details=dict(details or {}),
            recoverable=recoverable,
        )

    def representation(
        self,
        representation_type: RepresentationType,
        *,
        warnings: list[AdapterWarning],
        role: RepresentationRole = RepresentationRole.ORIGINAL,
        raw_path: str | Path | None = None,
        root_key: str = "source",
        url: str | None = None,
        sha256: str | None = None,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        network_required: bool = False,
        metadata_value: Mapping[str, Any] | None = None,
        field: str | None = None,
    ) -> RepresentationCandidate | None:
        path_reference: str | None = None
        if raw_path is not None and str(raw_path).strip():
            try:
                path_reference = self.path_ref(raw_path, root_key=root_key)
            except AdapterError as exc:
                warnings.append(
                    self.warning(
                        exc.code.value,
                        str(exc),
                        severity=WarningSeverity.ERROR,
                        field=field,
                        details=exc.details,
                    )
                )
                return None
            if not self.path_exists(path_reference, root_key=root_key):
                warnings.append(
                    self.warning(
                        "asset_path_missing",
                        f"Declared asset path does not exist: {root_key}:{path_reference}",
                        field=field,
                        path=path_reference,
                        details={"root_key": root_key},
                    )
                )
        valid_urls = [AnyUrl(value) for value in http_urls([url])]
        if path_reference is None and not valid_urls:
            return None
        return RepresentationCandidate(
            representation_type=representation_type,
            role=role,
            source_id=self.config.source_id,
            root_key=root_key,
            path_ref=path_reference,
            url=valid_urls[0] if valid_urls else None,
            sha256=string_or_none(sha256),
            mime_type=mime_type,
            size_bytes=size_bytes,
            network_required=network_required,
            metadata=dict(metadata_value or {}),
        )

    def identifiers(
        self,
        *,
        doi: Any = None,
        arxiv: Any = None,
        urls: Iterable[Any] = (),
    ) -> list[ExternalIdentifier]:
        candidates: list[tuple[str, str]] = []
        explicit_doi = string_or_none(doi)
        explicit_arxiv = string_or_none(arxiv)
        if explicit_doi:
            candidates.append(("doi", explicit_doi))
        if explicit_arxiv:
            candidates.append(("arxiv", explicit_arxiv))
        clean_urls = http_urls(urls)
        for url in clean_urls:
            doi_match = _DOI_RE.search(url)
            arxiv_match = _ARXIV_RE.search(url)
            if doi_match:
                candidates.append(("doi", doi_match.group(1).rstrip(".,);")))
            elif arxiv_match:
                candidates.append(("arxiv", arxiv_match.group(1)))
            else:
                candidates.append(("url", url))

        result: list[ExternalIdentifier] = []
        keys: set[tuple[str, str]] = set()
        for scheme, value in candidates:
            try:
                identifier = ExternalIdentifier(scheme=scheme, value=value)
            except ValidationError:
                continue
            key = (identifier.scheme, identifier.canonical or identifier.value)
            if key not in keys:
                keys.add(key)
                result.append(identifier)
        return result

    def repository_reference(self, url: str) -> AssetReference:
        scheme = "github" if "github.com/" in url.lower() else "url"
        return AssetReference(external_identifier=ExternalIdentifier(scheme=scheme, value=url))

    def relation(
        self,
        relation_type: RelationType,
        *,
        object_ref: AssetReference,
        field: str,
        excerpt: str | None = None,
        confidence: float | None = None,
        confirmed: bool = False,
        metadata_value: Mapping[str, Any] | None = None,
    ) -> RelationCandidate:
        return RelationCandidate(
            relation_type=relation_type,
            object=object_ref,
            evidence=RelationEvidence(
                source=self.config.source_id,
                path=self.manifest_source_path,
                field=field,
                excerpt=excerpt,
            ),
            confidence=confidence,
            confirmed=confirmed,
            metadata=dict(metadata_value or {}),
        )

    def source_label_reference(self, label: str) -> AssetReference:
        return AssetReference(
            external_identifier=ExternalIdentifier(
                scheme="source",
                value=f"{self.config.source_id}:label:{label.strip()}",
            )
        )

    def asset(
        self,
        *,
        source_record_id: str,
        title: str,
        title_zh: str | None = None,
        authors: list[str] | None = None,
        year: int | None = None,
        venue: str | None = None,
        abstract: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        analysis: Mapping[str, Any] | None = None,
        tags: list[str] | None = None,
        urls: Iterable[Any] = (),
        identifiers: list[ExternalIdentifier] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> NormalizedAsset:
        source_urls = [AnyUrl(value) for value in http_urls(urls)]
        return NormalizedAsset(
            asset_id=stable_uuid7(self.config.source_id, source_record_id),
            title=title,
            title_zh=title_zh,
            authors=authors or [],
            year=year,
            venue=venue,
            language="zh-CN",
            abstract=abstract,
            summary=summary,
            content=content,
            analysis=dict(analysis or {}),
            external_identifiers=identifiers
            or self.identifiers(urls=(str(url) for url in source_urls)),
            source_urls=source_urls,
            domain_slugs=self.config.domain_slugs,
            tags=list(dict.fromkeys(tags or [])),
            attributes=dict(attributes or {}),
        )

    def record(
        self,
        scanned: ScannedRecord,
        *,
        source_record_id: Any,
        asset: NormalizedAsset,
        representations: Iterable[RepresentationCandidate | None] = (),
        relations: Iterable[RelationCandidate] = (),
        warnings: Iterable[AdapterWarning] = (),
        source_fields: Mapping[str, Any] | None = None,
        outcome: ImportOutcome = ImportOutcome.READY,
    ) -> NormalizedRecord:
        record_id = string_or_none(source_record_id)
        if record_id is None:
            raise AdapterError(
                AdapterErrorCode.MISSING_SOURCE_RECORD_ID,
                f"Manifest record {scanned.index} has no source record id",
                path=self.manifest_absolute_path,
                details={"index": scanned.index},
            )
        return NormalizedRecord(
            source_id=self.config.source_id,
            source_record_id=record_id,
            source_path=scanned.source_path,
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            outcome=outcome,
            asset=asset,
            representations=[item for item in representations if item is not None],
            relations=list(relations),
            warnings=list(warnings),
            source_fields=dict(source_fields or {}),
            raw=scanned.raw,
        )


class AdapterRegistry:
    """Registry for built-ins and opt-in Python entry-point plugins."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[BaseAdapter]] = {}

    def register(
        self,
        name: str,
        adapter_type: type[_T] | None = None,
        *,
        replace: bool = False,
    ) -> type[_T] | Any:
        normalized = name.strip().lower().replace("-", "_")

        def decorator(candidate: type[_T]) -> type[_T]:
            if normalized in self._adapters and not replace:
                raise ValueError(f"Adapter '{normalized}' is already registered")
            if not issubclass(candidate, BaseAdapter):
                raise TypeError("Registered adapter must extend BaseAdapter")
            self._adapters[normalized] = candidate
            return candidate

        return decorator(adapter_type) if adapter_type is not None else decorator

    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def get(self, name: str) -> type[BaseAdapter]:
        normalized = name.strip().lower().replace("-", "_")
        try:
            return self._adapters[normalized]
        except KeyError as exc:
            raise AdapterError(
                AdapterErrorCode.UNKNOWN_ADAPTER,
                f"Unknown adapter '{name}'",
                details={"available": list(self.available())},
            ) from exc

    def create(
        self,
        name: str,
        config: SourceConfig | Mapping[str, Any],
        *,
        base_dir: Path | str | None = None,
    ) -> BaseAdapter:
        return self.get(name)(config, base_dir=base_dir)

    def discover_entry_points(
        self,
        group: str = "research_asset_hub.adapters",
        *,
        replace: bool = False,
    ) -> tuple[str, ...]:
        loaded: list[str] = []
        selected = metadata.entry_points().select(group=group)
        for entry_point in selected:
            try:
                adapter_type = entry_point.load()
                self.register(entry_point.name, adapter_type, replace=replace)
            except Exception as exc:
                raise AdapterError(
                    AdapterErrorCode.PLUGIN_LOAD_FAILED,
                    f"Could not load adapter plugin '{entry_point.name}': {exc}",
                    details={"entry_point": entry_point.value, "group": group},
                ) from exc
            loaded.append(entry_point.name)
        return tuple(loaded)
