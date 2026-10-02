"""Configurable adapter for straightforward JSON manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.domain.schemas import (
    AdapterWarning,
    AssetReference,
    ExternalIdentifier,
    NormalizedRecord,
)

from ._helpers import optional_year, require_text, warn_if_empty
from .base import BaseAdapter, get_in, http_urls, string_list, string_or_none
from .types import AdapterError, AdapterErrorCode, ScannedRecord

_FIELD_SELECTOR_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_GIT_COMMIT_RE = re.compile(r"[0-9A-Fa-f]{40}")
_GENERIC_REVISION_RE = re.compile(
    r"(?!.*(?:\.\.|//))(?!@$)(?!.*(?:/|\.|\.lock)$)"
    r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,254}"
)
_LICENSE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .()+:/_-]{0,126}")
_STATUS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


class GenericManifestAdapter(BaseAdapter):
    """Map a simple manifest through declarative dotted-field selectors.

    ``SourceConfig.options`` accepts:

    - ``record_selector``: dotted path to the record array (optional)
    - ``field_mapping``: normalized field name -> dotted source field
    - ``representations``: a list of path/url representation mappings
      optionally carrying ``commit_field`` or ``revision_field``,
      ``license_field``, and ``status_field``
    - ``relations``: a list of relation mappings
    """

    adapter_name = "generic_manifest"

    _DEFAULT_MAPPING = {
        "source_record_id": "id",
        "title": "title",
        "title_zh": "title_zh",
        "authors": "authors",
        "year": "year",
        "venue": "venue",
        "abstract": "abstract",
        "summary": "summary",
        "tags": "tags",
        "source_urls": "source_urls",
        "doi": "doi",
        "arxiv": "arxiv_id",
        "attributes": "attributes",
    }

    def _mapping(self) -> dict[str, str]:
        configured = self.config.options.get("field_mapping", {})
        if not isinstance(configured, Mapping):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "generic_manifest field_mapping must be an object",
            )
        result = dict(self._DEFAULT_MAPPING)
        result.update(
            {str(key): str(value) for key, value in configured.items() if string_or_none(value)}
        )
        return result

    @staticmethod
    def _representation_selector(
        spec: Mapping[str, Any],
        *,
        index: int,
        key: str,
    ) -> str | None:
        """Return one opt-in dotted selector without coercing unsafe config values."""

        if key not in spec:
            return None
        value = spec[key]
        if not isinstance(value, str):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                f"representations[{index}].{key} must be a non-empty dotted field selector",
                details={
                    "representation_index": index,
                    "option": key,
                    "actual_type": type(value).__name__,
                },
            )
        normalized = value.strip()
        if not normalized or not _FIELD_SELECTOR_RE.fullmatch(normalized):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                f"representations[{index}].{key} must be a non-empty dotted field selector",
                details={"representation_index": index, "option": key},
            )
        return normalized

    @staticmethod
    def _representation_text(
        raw: Mapping[str, Any],
        *,
        index: int,
        selector: str,
        label: str,
        pattern: re.Pattern[str],
        expectation: str,
    ) -> str:
        """Read a configured representation field as bounded, safe manifest text."""

        value = get_in(raw, selector)
        if not isinstance(value, str):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                f"representations[{index}] {label} must be {expectation}",
                details={
                    "representation_index": index,
                    "field": selector,
                    "actual_type": type(value).__name__,
                },
            )
        normalized = value.strip()
        if not normalized or not pattern.fullmatch(normalized):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                f"representations[{index}] {label} must be {expectation}",
                details={"representation_index": index, "field": selector},
            )
        return normalized

    def _repository_metadata(
        self,
        raw: Mapping[str, Any],
        spec: Mapping[str, Any],
        *,
        index: int,
        representation_type: RepresentationType,
        has_declared_location: bool,
    ) -> tuple[str | None, dict[str, str]]:
        """Map strictly configured repository revision/profile metadata."""

        commit_field = self._representation_selector(spec, index=index, key="commit_field")
        revision_field = self._representation_selector(
            spec,
            index=index,
            key="revision_field",
        )
        license_field = self._representation_selector(
            spec,
            index=index,
            key="license_field",
        )
        status_field = self._representation_selector(spec, index=index, key="status_field")
        if commit_field is not None and revision_field is not None:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                (
                    f"representations[{index}] cannot configure both "
                    "commit_field and revision_field"
                ),
                details={"representation_index": index},
            )
        repository_only = {
            "commit_field": commit_field,
            "revision_field": revision_field,
            "license_field": license_field,
            "status_field": status_field,
        }
        invalid_options = sorted(
            key
            for key, value in repository_only.items()
            if value is not None and representation_type != RepresentationType.REPOSITORY
        )
        if invalid_options:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                (
                    f"representations[{index}] options are valid only for "
                    f"repository representations: {', '.join(invalid_options)}"
                ),
                details={
                    "representation_index": index,
                    "options": invalid_options,
                },
            )
        if not has_declared_location:
            return None, {}

        revision: str | None = None
        if commit_field is not None:
            revision = self._representation_text(
                raw,
                index=index,
                selector=commit_field,
                label="commit",
                pattern=_GIT_COMMIT_RE,
                expectation="a 40-character hexadecimal Git commit",
            ).lower()
        elif revision_field is not None:
            revision = self._representation_text(
                raw,
                index=index,
                selector=revision_field,
                label="revision",
                pattern=_GENERIC_REVISION_RE,
                expectation="a safe revision token of at most 255 characters",
            )

        metadata: dict[str, str] = {}
        if license_field is not None:
            metadata["license"] = self._representation_text(
                raw,
                index=index,
                selector=license_field,
                label="license",
                pattern=_LICENSE_RE,
                expectation="a safe SPDX-style value of at most 127 characters",
            )
        if status_field is not None:
            metadata["status"] = self._representation_text(
                raw,
                index=index,
                selector=status_field,
                label="status",
                pattern=_STATUS_RE,
                expectation="a safe status token of at most 32 characters",
            )
        return revision, metadata

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        raw = record.raw
        mapping = self._mapping()
        warnings: list[AdapterWarning] = []
        record_id = require_text(
            self,
            record,
            get_in(raw, mapping["source_record_id"]),
            field=mapping["source_record_id"],
        )
        title = require_text(
            self,
            record,
            get_in(raw, mapping["title"]),
            field=mapping["title"],
        )
        configured_separators = self.config.options.get("author_separators", [";"])
        author_separators = (
            tuple(separator for separator in configured_separators if isinstance(separator, str))
            if isinstance(configured_separators, list)
            else (";",)
        )
        authors = string_list(
            get_in(raw, mapping.get("authors")),
            separators=author_separators or (";",),
        )
        warn_if_empty(
            self,
            warnings,
            authors,
            code="missing_authors",
            message="No authors were supplied by the manifest",
            field=mapping.get("authors", "authors"),
        )
        year = optional_year(
            self,
            get_in(raw, mapping.get("year")),
            warnings,
            field=mapping.get("year", "year"),
        )
        raw_urls = get_in(raw, mapping.get("source_urls"), [])
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        elif not isinstance(raw_urls, Sequence):
            raw_urls = []
        source_urls = http_urls(raw_urls)
        for key in ("paper_url", "source_url", "pdf_url"):
            path = mapping.get(key)
            if path:
                source_urls.extend(
                    url for url in http_urls([get_in(raw, path)]) if url not in source_urls
                )

        raw_attributes = get_in(raw, mapping.get("attributes"))
        if raw_attributes is None:
            mapped_attributes: dict[str, Any] = {}
        elif isinstance(raw_attributes, Mapping):
            mapped_attributes = {str(key): value for key, value in raw_attributes.items()}
        else:
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "generic_manifest attributes must be an object",
                details={
                    "field": mapping.get("attributes", "attributes"),
                    "actual_type": type(raw_attributes).__name__,
                },
            )
        # Keep adapter provenance under a reserved key after copying source-owned
        # attributes so a manifest cannot spoof the mapping used for this record.
        mapped_attributes["generic_mapping"] = mapping

        asset = self.asset(
            source_record_id=record_id,
            title=title,
            title_zh=string_or_none(get_in(raw, mapping.get("title_zh"))),
            authors=authors,
            year=year,
            venue=string_or_none(get_in(raw, mapping.get("venue"))),
            abstract=string_or_none(get_in(raw, mapping.get("abstract"))),
            summary=string_or_none(get_in(raw, mapping.get("summary"))),
            tags=string_list(get_in(raw, mapping.get("tags"))),
            urls=source_urls,
            identifiers=self.identifiers(
                doi=get_in(raw, mapping.get("doi")),
                arxiv=get_in(raw, mapping.get("arxiv")),
                urls=source_urls,
            ),
            attributes=mapped_attributes,
        )

        representations = []
        raw_specs = self.config.options.get("representations", [])
        if not isinstance(raw_specs, list):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "generic_manifest representations must be an array",
            )
        for index, spec in enumerate(raw_specs):
            if not isinstance(spec, Mapping):
                raise AdapterError(
                    AdapterErrorCode.INVALID_RECORD,
                    f"representations[{index}] must be an object",
                )
            try:
                representation_type = RepresentationType(str(spec["type"]))
                role = RepresentationRole(str(spec.get("role", "original")))
            except (KeyError, ValueError) as exc:
                raise AdapterError(
                    AdapterErrorCode.INVALID_RECORD,
                    f"representations[{index}] has an invalid type or role",
                ) from exc
            path_field = string_or_none(spec.get("path_field"))
            url_field = string_or_none(spec.get("url_field"))
            sha256_field = string_or_none(spec.get("sha256_field"))
            raw_path = get_in(raw, path_field) if path_field else None
            raw_url = get_in(raw, url_field) if url_field else None
            has_declared_location = bool(string_or_none(raw_path)) or bool(
                string_or_none(raw_url)
            )
            revision, representation_metadata = self._repository_metadata(
                raw,
                spec,
                index=index,
                representation_type=representation_type,
                has_declared_location=has_declared_location,
            )
            fixed_metadata = spec.get("metadata", {})
            if not isinstance(fixed_metadata, Mapping):
                raise AdapterError(
                    AdapterErrorCode.INVALID_RECORD,
                    f"representations[{index}].metadata must be an object",
                )
            candidate = self.representation(
                representation_type,
                warnings=warnings,
                role=role,
                raw_path=raw_path,
                root_key=str(spec.get("root_key", "source")),
                url=string_or_none(raw_url) if url_field else None,
                sha256=(
                    string_or_none(get_in(raw, sha256_field))
                    if sha256_field
                    else None
                ),
                revision=revision,
                mime_type=string_or_none(spec.get("mime_type")),
                network_required=bool(spec.get("network_required", False)),
                metadata_value=(
                    dict(fixed_metadata) | {"mapping_index": index} | representation_metadata
                ),
                field=path_field or url_field,
            )
            if candidate is not None:
                representations.append(candidate)

        relations = []
        raw_relations = self.config.options.get("relations", [])
        if not isinstance(raw_relations, list):
            raise AdapterError(
                AdapterErrorCode.INVALID_RECORD,
                "generic_manifest relations must be an array",
            )
        for index, spec in enumerate(raw_relations):
            if not isinstance(spec, Mapping):
                continue
            target_field = string_or_none(spec.get("target_field"))
            if target_field is None:
                continue
            targets = get_in(raw, target_field)
            if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
                targets = [] if targets is None else [targets]
            try:
                relation_type = RelationType(str(spec.get("type", "related_to")))
            except ValueError as exc:
                raise AdapterError(
                    AdapterErrorCode.INVALID_RECORD,
                    f"relations[{index}] has an invalid relation type",
                ) from exc
            target_kind = str(spec.get("target_kind", "source_record_id"))
            for target in targets:
                target_text = string_or_none(target)
                if target_text is None:
                    continue
                if target_kind == "source_record_id":
                    object_ref = AssetReference(source_record_id=target_text)
                else:
                    scheme = str(spec.get("scheme", "source"))
                    object_ref = AssetReference(
                        external_identifier=ExternalIdentifier(
                            scheme=scheme,
                            value=target_text,
                        )
                    )
                relations.append(
                    self.relation(
                        relation_type,
                        object_ref=object_ref,
                        field=target_field,
                        excerpt=target_text,
                    )
                )

        return self.record(
            record,
            source_record_id=record_id,
            asset=asset,
            representations=representations,
            relations=relations,
            warnings=warnings,
            source_fields={
                "record_selector": self.config.options.get("record_selector"),
                "field_mapping": mapping,
            },
        )
