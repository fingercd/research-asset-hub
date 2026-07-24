"""Adapter for the world-model catalog and its status sidecars."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.domain.enums import RelationType, RepresentationRole, RepresentationType
from packages.domain.schemas import AdapterWarning, NormalizedRecord

from ._helpers import (
    array_from_document,
    grouped_mapping,
    mapping_index,
    optional_year,
    require_text,
    warn_if_empty,
)
from .base import BaseAdapter, http_urls, string_list, string_or_none
from .types import ScannedRecord


class WorldModelAdapter(BaseAdapter):
    adapter_name = "world_model"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prepared = False
        self._repositories: dict[str, list[dict[str, Any]]] = {}
        self._paper_status: dict[str, dict[str, Any]] = {}
        self._repository_status: dict[str, list[dict[str, Any]]] = {}
        self._annotation_status: dict[str, dict[str, Any]] = {}

    def prepare(self, records: Sequence[ScannedRecord]) -> None:
        repositories = array_from_document(self.load_auxiliary("repositories") or [])
        paper_status = array_from_document(self.load_auxiliary("paper_download_status") or [])
        repository_status = array_from_document(
            self.load_auxiliary("repository_download_status") or []
        )
        annotation_status = array_from_document(self.load_auxiliary("annotation_status") or [])
        self._repositories = grouped_mapping(repositories, "paper_id")
        self._paper_status = mapping_index(paper_status, "id")
        self._repository_status = grouped_mapping(repository_status, "paper_id")
        self._annotation_status = mapping_index(annotation_status, "paper_id")
        self._prepared = True

    def _ensure_prepared(self) -> None:
        if not self._prepared:
            self.prepare(self.scan())

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        self._ensure_prepared()
        raw = record.raw
        warnings: list[AdapterWarning] = []
        record_id = require_text(self, record, raw.get("id"), field="id")
        title = require_text(self, record, raw.get("title"), field="title")
        authors = string_list(raw.get("authors"))
        warn_if_empty(
            self,
            warnings,
            authors,
            code="missing_authors",
            message="The world-model catalog does not provide paper authors",
            field="authors",
        )
        year = optional_year(self, raw.get("year"), warnings)
        source_urls = http_urls(
            [
                raw.get("page_url"),
                *(raw.get("pdf_urls") or []),
                raw.get("official_repo"),
            ]
        )
        analysis = {
            key: raw.get(key)
            for key in (
                "problem",
                "method",
                "experiments",
                "limitations",
                "relations",
            )
            if raw.get(key) not in (None, "")
        }
        asset = self.asset(
            source_record_id=record_id,
            title=title,
            authors=authors,
            year=year,
            venue=string_or_none(raw.get("venue")),
            summary=string_or_none(raw.get("summary")),
            analysis=analysis,
            tags=string_list([raw.get("module"), raw.get("kind"), raw.get("status")]),
            urls=source_urls,
            attributes={
                "module": raw.get("module"),
                "status": raw.get("status"),
                "kind": raw.get("kind"),
            },
        )

        paper_status = self._paper_status.get(record_id, {})
        pdf_urls = http_urls(raw.get("pdf_urls") or [])
        representations = [
            self.representation(
                RepresentationType.PDF,
                warnings=warnings,
                role=RepresentationRole.PRIMARY,
                raw_path=paper_status.get("path"),
                root_key="pdf",
                url=pdf_urls[0] if pdf_urls else None,
                sha256=string_or_none(paper_status.get("sha256")),
                mime_type="application/pdf",
                size_bytes=paper_status.get("bytes")
                if isinstance(paper_status.get("bytes"), int)
                else None,
                network_required=not bool(paper_status.get("path")),
                field="paper_download_status.path",
                metadata_value={
                    "status": paper_status.get("status"),
                    "pages": paper_status.get("pages"),
                },
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
                RepresentationType.ANALYSIS,
                warnings=warnings,
                role=RepresentationRole.ANALYSIS,
                raw_path=self.manifest_source_path,
                metadata_value={"locator": f"[id={record_id}]"},
                field="summary|problem|method",
            ),
        ]

        repo_catalog = self._repositories.get(record_id, [])
        repo_status = self._repository_status.get(record_id, [])
        by_url = {
            string_or_none(item.get("url")): item
            for item in repo_status
            if string_or_none(item.get("url"))
        }
        relations = []
        for index, repo in enumerate(repo_catalog):
            repo_url = string_or_none(repo.get("url"))
            if repo_url is None:
                continue
            status = by_url.get(repo_url, {})
            representations.append(
                self.representation(
                    RepresentationType.REPOSITORY,
                    warnings=warnings,
                    role=RepresentationRole.OFFICIAL,
                    raw_path=status.get("path"),
                    root_key="repository",
                    url=repo_url,
                    network_required=not bool(status.get("path")),
                    field=f"repositories.{index}.url",
                    metadata_value={
                        "commit": status.get("commit"),
                        "branch": repo.get("branch"),
                        "license": repo.get("license"),
                        "language": repo.get("language"),
                        "repo_type": repo.get("repo_type"),
                        "core_files": repo.get("core_files"),
                        "status": status.get("status"),
                    },
                )
            )
            relations.append(
                self.relation(
                    RelationType.IMPLEMENTED_BY,
                    object_ref=self.repository_reference(repo_url),
                    field=f"repositories.{index}.url",
                    excerpt=string_or_none(repo.get("note")) or repo_url,
                    confidence=1.0,
                    confirmed=True,
                    metadata_value={"repo_type": repo.get("repo_type")},
                )
            )

        annotation = self._annotation_status.get(record_id)
        if annotation:
            representations.append(
                self.representation(
                    RepresentationType.GUIDE,
                    warnings=warnings,
                    role=RepresentationRole.GUIDE,
                    raw_path=f"{record_id}/ANNOTATION_GUIDE.md",
                    root_key="annotation",
                    mime_type="text/markdown",
                    field="annotation_status",
                    metadata_value={
                        "annotated_copies": annotation.get("annotated_copies"),
                        "commit": annotation.get("commit"),
                    },
                )
            )
            for index, file_info in enumerate(annotation.get("files") or []):
                if not isinstance(file_info, Mapping) or not file_info.get("notes_path"):
                    continue
                representations.append(
                    self.representation(
                        RepresentationType.ANNOTATION,
                        warnings=warnings,
                        role=RepresentationRole.ANNOTATED,
                        raw_path=f"{record_id}/{file_info['notes_path']}",
                        root_key="annotation",
                        mime_type="text/markdown",
                        field=f"annotation_status.files.{index}.notes_path",
                        metadata_value={
                            "source": file_info.get("path"),
                            "role": file_info.get("role"),
                            "mode": file_info.get("mode"),
                        },
                    )
                )

        narrative_relation = string_or_none(raw.get("relations"))
        if narrative_relation:
            relations.append(
                self.relation(
                    RelationType.RELATED_TO,
                    object_ref=self.source_label_reference(narrative_relation),
                    field="relations",
                    excerpt=narrative_relation,
                    confidence=0.35,
                )
            )
            warnings.append(
                self.warning(
                    "narrative_relation_unresolved",
                    "The source relation is narrative text and needs entity resolution",
                    field="relations",
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
                "repositories": self.config.auxiliary.get("repositories"),
                "paper_status": self.config.auxiliary.get("paper_download_status"),
                "repository_status": self.config.auxiliary.get("repository_download_status"),
                "annotation_status": self.config.auxiliary.get("annotation_status"),
            },
        )
