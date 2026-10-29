"""Adapter for the Agent paper library."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.domain.schemas import AdapterWarning, AssetReference, NormalizedRecord

from ._helpers import optional_year, require_text, strip_leading_parents, warn_if_empty
from .base import BaseAdapter, http_urls, string_list, string_or_none
from .types import ScannedRecord


class AgentAdapter(BaseAdapter):
    adapter_name = "agent"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._title_to_id: dict[str, str] = {}

    def prepare(self, records: Sequence[ScannedRecord]) -> None:
        self._title_to_id = {}
        for scanned in records:
            title = string_or_none(scanned.raw.get("title"))
            record_id = string_or_none(scanned.raw.get("id") or scanned.raw.get("slug"))
            if title and record_id:
                self._title_to_id[title.casefold()] = record_id

    def _lineage_relations(
        self,
        raw: Mapping[str, Any],
        warnings: list[AdapterWarning],
    ) -> list[Any]:
        relations = []
        for field, relation_type in (
            ("before", RelationType.FOLLOWS),
            ("after", RelationType.PRECEDES),
        ):
            values = raw.get(field)
            if not isinstance(values, list):
                continue
            for index, label in enumerate(values):
                text = string_or_none(label)
                if text is None:
                    continue
                matched_id = self._title_to_id.get(text.casefold())
                object_ref = (
                    AssetReference(source_record_id=matched_id)
                    if matched_id
                    else self.source_label_reference(text)
                )
                if matched_id is None:
                    warnings.append(
                        self.warning(
                            "unresolved_lineage_label",
                            f"Lineage label did not exactly match a source title: {text}",
                            field=f"{field}.{index}",
                            details={"label": text},
                        )
                    )
                relations.append(
                    self.relation(
                        relation_type,
                        object_ref=object_ref,
                        field=f"{field}.{index}",
                        excerpt=text,
                        confidence=1.0 if matched_id else 0.5,
                    )
                )
        return relations

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        raw = record.raw
        warnings: list[AdapterWarning] = []
        record_id = require_text(
            self,
            record,
            raw.get("id") or raw.get("slug"),
            field="id|slug",
        )
        title = require_text(self, record, raw.get("title"), field="title")
        authors = string_list(raw.get("authors"))
        warn_if_empty(
            self,
            warnings,
            authors,
            code="missing_authors",
            message="No authors were supplied by the Agent manifest",
            field="authors",
        )
        year = optional_year(self, raw.get("year"), warnings)
        source_urls = http_urls(
            [
                *(raw.get("source_urls") or []),
                raw.get("official"),
                raw.get("pdf"),
            ]
        )
        analysis = {
            key: raw[key]
            for key in (
                "question",
                "mechanism",
                "steps",
                "contributions",
                "evidence",
                "limitations",
                "reading_questions",
            )
            if raw.get(key) not in (None, "", [])
        }
        asset = self.asset(
            source_record_id=record_id,
            title=title,
            title_zh=string_or_none(raw.get("title_zh")),
            authors=authors,
            year=year,
            venue=string_or_none(raw.get("venue")),
            abstract=string_or_none(raw.get("abstract")),
            summary=string_or_none(raw.get("question") or raw.get("abstract")),
            analysis=analysis,
            tags=string_list(
                [
                    *(raw.get("tags") or []),
                    raw.get("module"),
                    raw.get("paper_kind"),
                    raw.get("status"),
                ]
            ),
            urls=source_urls,
            identifiers=self.identifiers(
                doi=raw.get("doi"),
                arxiv=raw.get("arxiv_id"),
                urls=source_urls,
            ),
            attributes={
                "module": raw.get("module"),
                "paper_kind": raw.get("paper_kind"),
                "status": raw.get("status"),
                "first_publication_date": raw.get("first_publication_date"),
                "source_note": raw.get("source_note"),
            },
        )

        representations = [
            self.representation(
                RepresentationType.PDF,
                warnings=warnings,
                role=RepresentationRole.PRIMARY,
                raw_path=strip_leading_parents(raw.get("local_pdf")),
                root_key="pdf",
                url=string_or_none(raw.get("pdf")),
                mime_type="application/pdf",
                network_required=not bool(raw.get("local_pdf")),
                field="local_pdf",
            ),
            self.representation(
                RepresentationType.HTML,
                warnings=warnings,
                role=RepresentationRole.DERIVED,
                raw_path=f"papers/{raw.get('slug') or record_id}/index.html",
                root_key="html",
                mime_type="text/html",
                field="slug",
            ),
            self.representation(
                RepresentationType.REPOSITORY,
                warnings=warnings,
                role=RepresentationRole.OFFICIAL,
                raw_path=strip_leading_parents(raw.get("local_code")),
                root_key="repository",
                url=string_or_none(raw.get("official_code")),
                network_required=not bool(raw.get("local_code")),
                metadata_value={
                    "label": raw.get("official_code_label"),
                },
                field="local_code",
            ),
            self.representation(
                RepresentationType.GUIDE,
                warnings=warnings,
                role=RepresentationRole.ANNOTATED,
                raw_path=strip_leading_parents(raw.get("annotation")),
                root_key="annotation",
                mime_type="text/markdown",
                field="annotation",
            ),
            self.representation(
                RepresentationType.ANALYSIS,
                warnings=warnings,
                role=RepresentationRole.ANALYSIS,
                raw_path=self.manifest_source_path,
                metadata_value={"locator": f"papers[id={record_id}]"},
                field="analysis",
            ),
        ]
        relations = self._lineage_relations(raw, warnings)
        official_code = http_urls([raw.get("official_code")])
        if official_code:
            relations.append(
                self.relation(
                    RelationType.IMPLEMENTED_BY,
                    object_ref=self.repository_reference(official_code[0]),
                    field="official_code",
                    excerpt=official_code[0],
                    confidence=1.0,
                    confirmed=True,
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
                "id": "id|slug",
                "title": "title",
                "analysis": list(analysis),
                "pdf": "local_pdf|pdf",
                "repository": "local_code|official_code",
                "guide": "annotation",
                "relations": ["before", "after"],
            },
        )
