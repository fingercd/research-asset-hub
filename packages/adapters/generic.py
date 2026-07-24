"""Configurable adapter for straightforward JSON manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

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


class GenericManifestAdapter(BaseAdapter):
    """Map a simple manifest through declarative dotted-field selectors.

    ``SourceConfig.options`` accepts:

    - ``record_selector``: dotted path to the record array (optional)
    - ``field_mapping``: normalized field name -> dotted source field
    - ``representations``: a list of path/url representation mappings
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
            attributes={
                "generic_mapping": mapping,
            },
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
            candidate = self.representation(
                representation_type,
                warnings=warnings,
                role=role,
                raw_path=get_in(raw, path_field) if path_field else None,
                root_key=str(spec.get("root_key", "source")),
                url=string_or_none(get_in(raw, url_field)) if url_field else None,
                sha256=string_or_none(get_in(raw, string_or_none(spec.get("sha256_field")))),
                mime_type=string_or_none(spec.get("mime_type")),
                network_required=bool(spec.get("network_required", False)),
                metadata_value={"mapping_index": index},
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
