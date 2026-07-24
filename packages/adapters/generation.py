"""Adapter for the image/video generation paper library."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.domain.schemas import AdapterWarning, NormalizedRecord

from ._helpers import (
    array_from_document,
    mapping_index,
    mapping_or_empty,
    optional_year,
    require_text,
    strip_prefix,
    warn_if_empty,
)
from .base import BaseAdapter, http_urls, string_list, string_or_none
from .types import ScannedRecord


class GenerationAdapter(BaseAdapter):
    adapter_name = "generation"
    _LIBRARY_PREFIX = "outputs/paper_library"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prepared = False
        self._analyses: dict[str, dict[str, Any]] = {}
        self._code_map: dict[str, dict[str, Any]] = {}

    def prepare(self, records: Sequence[ScannedRecord]) -> None:
        analyses = array_from_document(self.load_auxiliary("analyses") or [])
        code_map = array_from_document(self.load_auxiliary("code_map") or [])
        self._analyses = mapping_index(analyses, "paper_id")
        self._code_map = mapping_index(code_map, "paper_id")
        self._prepared = True

    def _ensure_prepared(self) -> None:
        if not self._prepared:
            self.prepare(self.scan())

    def _library_path(self, value: Any) -> str | None:
        return strip_prefix(value, self._LIBRARY_PREFIX)

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        self._ensure_prepared()
        raw = record.raw
        warnings: list[AdapterWarning] = []
        record_id = require_text(
            self,
            record,
            raw.get("paper_id") or raw.get("slug"),
            field="paper_id|slug",
        )
        slug = require_text(
            self,
            record,
            raw.get("slug") or raw.get("paper_id"),
            field="slug|paper_id",
        )
        title = require_text(self, record, raw.get("title"), field="title")
        analysis = self._analyses.get(record_id) or {}
        code = self._code_map.get(record_id) or {}
        authors = string_list(raw.get("authors"))
        warn_if_empty(
            self,
            warnings,
            authors,
            code="missing_authors",
            message="The generation manifest does not provide paper authors",
            field="authors",
        )
        year = optional_year(self, raw.get("year"), warnings)
        source_urls = http_urls(
            [
                raw.get("paper_page_url"),
                raw.get("pdf_source_page_url"),
                raw.get("pdf_url"),
                *(raw.get("alternative_pdf_urls") or []),
                code.get("paper_url"),
            ]
        )
        normalized_analysis = {
            key: analysis.get(key)
            for key in (
                "background",
                "method",
                "modules",
                "training",
                "inference",
                "contributions",
                "limitations",
                "evaluation",
                "relations",
                "recommended_reading",
                "tags",
                "evidence_notes",
            )
            if analysis.get(key) not in (None, "", [])
        }
        asset = self.asset(
            source_record_id=record_id,
            title=title,
            authors=authors,
            year=year,
            venue=string_or_none(raw.get("venue_status")),
            summary=string_or_none(analysis.get("summary") or raw.get("summary")),
            analysis=normalized_analysis,
            tags=string_list(
                [
                    *(analysis.get("tags") or []),
                    raw.get("category"),
                    raw.get("roadmap_section"),
                    raw.get("priority"),
                ]
            ),
            urls=source_urls,
            attributes={
                "slug": slug,
                "category": raw.get("category"),
                "roadmap_section": raw.get("roadmap_section"),
                "roadmap_line": raw.get("roadmap_line"),
                "priority": raw.get("priority"),
                "year_label": raw.get("year_label"),
                "module": raw.get("module"),
                "venue_status": raw.get("venue_status"),
                "code_status": code.get("status"),
            },
        )

        download = mapping_or_empty(raw.get("download"))
        local_pdf = self._library_path(raw.get("local_pdf_path"))
        local_repo = self._library_path(code.get("local_path"))
        representations = [
            self.representation(
                RepresentationType.PDF,
                warnings=warnings,
                role=RepresentationRole.PRIMARY,
                raw_path=local_pdf,
                root_key="pdf",
                url=string_or_none(raw.get("pdf_url")),
                sha256=string_or_none(download.get("sha256")),
                mime_type="application/pdf",
                size_bytes=download.get("bytes")
                if isinstance(download.get("bytes"), int)
                else None,
                network_required=not bool(local_pdf),
                field="local_pdf_path",
                metadata_value={
                    "status": download.get("status"),
                    "page_count": download.get("page_count"),
                    "final_url": download.get("final_url"),
                },
            ),
            self.representation(
                RepresentationType.HTML,
                warnings=warnings,
                role=RepresentationRole.DERIVED,
                raw_path=f"pages/{slug}.html",
                root_key="html",
                mime_type="text/html",
                field="slug",
            ),
            self.representation(
                RepresentationType.REPOSITORY,
                warnings=warnings,
                role=RepresentationRole.OFFICIAL,
                raw_path=local_repo,
                root_key="repository",
                url=string_or_none(code.get("repo_url")),
                network_required=not bool(local_repo),
                field="code_map.local_path",
                metadata_value={
                    key: code.get(key)
                    for key in (
                        "repo",
                        "officiality",
                        "verification",
                        "commit",
                        "status",
                        "entrypoints",
                        "notes",
                    )
                    if code.get(key) not in (None, "", [])
                },
            ),
            self.representation(
                RepresentationType.ANALYSIS,
                warnings=warnings,
                role=RepresentationRole.ANALYSIS,
                raw_path=self.config.auxiliary.get("analyses"),
                metadata_value={"locator": f"papers[paper_id={record_id}]"},
                field="analyses",
            ),
        ]
        if local_repo:
            representations.append(
                self.representation(
                    RepresentationType.GUIDE,
                    warnings=warnings,
                    role=RepresentationRole.GUIDE,
                    raw_path=f"{local_repo}/CODE_GUIDE.zh-CN.md",
                    root_key="annotation",
                    mime_type="text/markdown",
                    field="code_map.local_path",
                )
            )
        for index, modified_path in enumerate(code.get("modified_files") or []):
            if isinstance(modified_path, Mapping):
                path_value = modified_path.get("path") or modified_path.get("output")
            else:
                path_value = modified_path
            if path_value:
                representations.append(
                    self.representation(
                        RepresentationType.ANNOTATION,
                        warnings=warnings,
                        role=RepresentationRole.ANNOTATED,
                        raw_path=f"{local_repo}/{path_value}" if local_repo else path_value,
                        root_key="annotation",
                        field=f"code_map.modified_files.{index}",
                    )
                )

        relations = []
        repo_urls = http_urls([code.get("repo_url")])
        if repo_urls:
            relations.append(
                self.relation(
                    RelationType.IMPLEMENTED_BY,
                    object_ref=self.repository_reference(repo_urls[0]),
                    field="code_map.repo_url",
                    excerpt=string_or_none(code.get("verification")) or repo_urls[0],
                    confidence=1.0,
                    confirmed=code.get("status") in {"official", "cloned", "downloaded"},
                    metadata_value={"officiality": code.get("officiality")},
                )
            )

        lineage = analysis.get("relations")
        if isinstance(lineage, Mapping):
            relation_values = 0
            for field, relation_type in (
                ("predecessors", RelationType.FOLLOWS),
                ("successors", RelationType.PRECEDES),
            ):
                for index, label in enumerate(lineage.get(field) or []):
                    text = string_or_none(label)
                    if text is None:
                        continue
                    relation_values += 1
                    relations.append(
                        self.relation(
                            relation_type,
                            object_ref=self.source_label_reference(text),
                            field=f"analyses.relations.{field}.{index}",
                            excerpt=text,
                            confidence=0.4,
                        )
                    )
            if relation_values:
                warnings.append(
                    self.warning(
                        "narrative_lineage_unresolved",
                        "Lineage labels are preserved as candidates pending entity resolution",
                        field="analyses.relations",
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
                "paper": self.manifest_source_path,
                "analyses": self.config.auxiliary.get("analyses"),
                "code_map": self.config.auxiliary.get("code_map"),
                "id": "paper_id|slug",
            },
        )
