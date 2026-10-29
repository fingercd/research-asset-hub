"""Adapter for the fine-tuning paper library."""

from __future__ import annotations

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.domain.schemas import AdapterWarning, NormalizedRecord

from ._helpers import optional_year, require_text
from .base import BaseAdapter, http_urls, string_list, string_or_none
from .types import ScannedRecord


class FineTuningAdapter(BaseAdapter):
    adapter_name = "fine_tuning"

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        raw = record.raw
        warnings: list[AdapterWarning] = []
        record_id = require_text(self, record, raw.get("id"), field="id")
        title = require_text(self, record, raw.get("title"), field="title")
        authors = string_list(raw.get("authors"), separators=(";",))
        if not authors:
            warnings.append(
                self.warning(
                    "missing_authors",
                    "No authors were supplied by the fine-tuning manifest",
                    field="authors",
                )
            )
        year = optional_year(self, raw.get("year"), warnings)
        source_urls = http_urls(
            [raw.get("paper_page_url"), raw.get("pdf_url"), raw.get("code_evidence_url")]
        )
        analysis = {
            key: raw.get(key)
            for key in (
                "problem",
                "method_points",
                "experiments",
                "key_findings",
                "limitations",
                "reading_notes",
            )
            if raw.get(key) not in (None, "")
        }
        asset = self.asset(
            source_record_id=record_id,
            title=title,
            authors=authors,
            year=year,
            venue=string_or_none(raw.get("venue")),
            abstract=string_or_none(raw.get("abstract_zh")),
            summary=string_or_none(raw.get("abstract_zh")),
            analysis=analysis,
            tags=string_list([raw.get("category"), raw.get("code_status")]),
            urls=source_urls,
            attributes={
                "category": raw.get("category"),
                "pdf_status": raw.get("pdf_status"),
                "code_status": raw.get("code_status"),
                "segment": raw.get("_segment"),
            },
        )

        representations = [
            self.representation(
                RepresentationType.PDF,
                warnings=warnings,
                role=RepresentationRole.PRIMARY,
                raw_path=raw.get("pdf_local"),
                root_key="pdf",
                url=string_or_none(raw.get("pdf_url")),
                mime_type="application/pdf",
                network_required=not bool(raw.get("pdf_local")),
                field="pdf_local",
                metadata_value={"status": raw.get("pdf_status")},
            ),
            self.representation(
                RepresentationType.HTML,
                warnings=warnings,
                role=RepresentationRole.DERIVED,
                raw_path=f"papers/{record_id}.html",
                root_key="html",
                mime_type="text/html",
                field="id",
            ),
            self.representation(
                RepresentationType.REPOSITORY,
                warnings=warnings,
                role=RepresentationRole.OFFICIAL,
                raw_path=raw.get("repo_local"),
                root_key="repository",
                url=string_or_none(raw.get("repo_url")),
                network_required=not bool(raw.get("repo_local")),
                field="repo_local",
                metadata_value={
                    "commit": raw.get("repo_commit"),
                    "license": raw.get("license"),
                    "status": raw.get("code_status"),
                },
            ),
            self.representation(
                RepresentationType.GUIDE,
                warnings=warnings,
                role=RepresentationRole.GUIDE,
                raw_path=raw.get("guide_local"),
                root_key="annotation",
                mime_type="text/markdown",
                field="guide_local",
            ),
            self.representation(
                RepresentationType.ANALYSIS,
                warnings=warnings,
                role=RepresentationRole.ANALYSIS,
                raw_path=self.manifest_source_path,
                metadata_value={"locator": f"[id={record_id}]"},
                field="analysis",
            ),
        ]
        for index, annotated_path in enumerate(raw.get("annotated_files") or []):
            representations.append(
                self.representation(
                    RepresentationType.ANNOTATION,
                    warnings=warnings,
                    role=RepresentationRole.ANNOTATED,
                    raw_path=annotated_path,
                    root_key="annotation",
                    field=f"annotated_files.{index}",
                )
            )

        relations = []
        repository_urls = http_urls([raw.get("repo_url")])
        if repository_urls:
            relations.append(
                self.relation(
                    RelationType.IMPLEMENTED_BY,
                    object_ref=self.repository_reference(repository_urls[0]),
                    field="repo_url",
                    excerpt=string_or_none(raw.get("code_evidence_url")) or repository_urls[0],
                    confidence=1.0,
                    confirmed=string_or_none(raw.get("code_status")) == "official_code",
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
                "pdf": "pdf_local|pdf_url",
                "repository": "repo_local|repo_url",
                "guide": "guide_local|annotated_files",
                "analysis": list(analysis),
            },
        )
