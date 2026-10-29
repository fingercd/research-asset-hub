"""Adapter for the infrastructure paper library manifest."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.domain.schemas import AdapterWarning, NormalizedRecord

from ._helpers import mapping_or_empty, optional_year, require_text, warn_if_empty
from .base import BaseAdapter, http_urls, string_list, string_or_none
from .types import ScannedRecord


class InfraAdapter(BaseAdapter):
    adapter_name = "infra"

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        raw = record.raw
        warnings: list[AdapterWarning] = []
        record_id = require_text(self, record, raw.get("id"), field="id")
        title = require_text(self, record, raw.get("title"), field="title")
        analysis: dict[str, Any] = mapping_or_empty(raw.get("analysis"))
        pdf: dict[str, Any] = mapping_or_empty(raw.get("pdf"))
        code: dict[str, Any] = mapping_or_empty(raw.get("code"))
        authors: list[str] = []
        warn_if_empty(
            self,
            warnings,
            authors,
            code="missing_authors",
            message="The Infra manifest does not provide paper authors",
            field="authors",
        )
        source_urls = http_urls(
            [
                raw.get("source_url"),
                analysis.get("source_url"),
                *[link.get("url") for link in raw.get("links", []) if isinstance(link, Mapping)],
            ]
        )
        year = optional_year(self, raw.get("year") or analysis.get("year"), warnings)
        asset = self.asset(
            source_record_id=record_id,
            title=title,
            authors=authors,
            year=year,
            venue=string_or_none(raw.get("venue") or analysis.get("venue")),
            summary=string_or_none(analysis.get("one_sentence") or raw.get("curated_summary")),
            analysis={
                **dict(analysis),
                "curated_summary": raw.get("curated_summary"),
                "curated_limitations": raw.get("curated_limitations"),
            },
            tags=string_list(
                [
                    raw.get("module"),
                    raw.get("kind"),
                    raw.get("priority"),
                ]
            ),
            urls=source_urls,
            attributes={
                "module_id": raw.get("module_id"),
                "module": raw.get("module"),
                "kind": raw.get("kind"),
                "priority": raw.get("priority"),
                "year_venue_raw": raw.get("year_venue_raw"),
            },
        )

        representations = [
            self.representation(
                RepresentationType.PDF,
                warnings=warnings,
                role=RepresentationRole.PRIMARY,
                raw_path=pdf.get("pdf_path"),
                root_key="pdf",
                url=string_or_none(pdf.get("resolved_url")),
                sha256=string_or_none(pdf.get("sha256")),
                mime_type="application/pdf",
                size_bytes=pdf.get("bytes") if isinstance(pdf.get("bytes"), int) else None,
                field="pdf.pdf_path",
                metadata_value={
                    "status": pdf.get("status"),
                    "pages": pdf.get("pages"),
                    "resolution": pdf.get("resolution"),
                },
            ),
            self.representation(
                RepresentationType.HTML,
                warnings=warnings,
                role=RepresentationRole.DERIVED,
                raw_path=f"papers/{record_id}/index.html",
                root_key="html",
                mime_type="text/html",
                field="id",
            ),
            self.representation(
                RepresentationType.REPOSITORY,
                warnings=warnings,
                role=RepresentationRole.OFFICIAL,
                raw_path=code.get("repo_path"),
                root_key="repository",
                url=string_or_none(code.get("repo_url")),
                network_required=not bool(code.get("repo_path")),
                field="code.repo_path",
                metadata_value={
                    key: code.get(key)
                    for key in (
                        "repo",
                        "commit",
                        "branch",
                        "license",
                        "status",
                        "relationship",
                        "subpath",
                    )
                    if code.get(key) not in (None, "")
                },
            ),
            self.representation(
                RepresentationType.GUIDE,
                warnings=warnings,
                role=RepresentationRole.GUIDE,
                raw_path=code.get("guide_path"),
                root_key="annotation",
                mime_type="text/html",
                field="code.guide_path",
            ),
            self.representation(
                RepresentationType.MARKDOWN,
                warnings=warnings,
                role=RepresentationRole.GUIDE,
                raw_path=code.get("guide_markdown_path"),
                root_key="annotation",
                mime_type="text/markdown",
                field="code.guide_markdown_path",
            ),
            self.representation(
                RepresentationType.ANALYSIS,
                warnings=warnings,
                role=RepresentationRole.ANALYSIS,
                raw_path=self.manifest_source_path,
                metadata_value={"locator": f"items[id={record_id}].analysis"},
                field="analysis",
            ),
        ]
        for index, annotated in enumerate(code.get("annotated_files", [])):
            if not isinstance(annotated, Mapping):
                continue
            representations.append(
                self.representation(
                    RepresentationType.ANNOTATION,
                    warnings=warnings,
                    role=RepresentationRole.ANNOTATED,
                    raw_path=annotated.get("output"),
                    root_key="annotation",
                    field=f"code.annotated_files.{index}.output",
                    metadata_value={
                        key: annotated.get(key)
                        for key in ("source", "reason", "source_lines")
                        if annotated.get(key) is not None
                    },
                )
            )

        relations = []
        repository_urls = http_urls([code.get("repo_url")])
        if repository_urls:
            relations.append(
                self.relation(
                    RelationType.IMPLEMENTED_BY,
                    object_ref=self.repository_reference(repository_urls[0]),
                    field="code.repo_url",
                    excerpt=string_or_none(code.get("evidence")) or repository_urls[0],
                    confidence=1.0,
                    confirmed=True,
                    metadata_value={"relationship": code.get("relationship")},
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
                "id": "id",
                "title": "title|analysis.title",
                "analysis": "analysis",
                "pdf": "pdf",
                "repository": "code",
            },
        )
