"""Adapter for the reinforcement-learning paper site and sidecar catalogs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packages.domain.enums import (
    RelationType,
    RepresentationRole,
    RepresentationType,
    WarningSeverity,
)
from packages.domain.schemas import AdapterWarning, NormalizedRecord

from ._helpers import (
    array_from_document,
    mapping_index,
    optional_year,
    repository_folder,
    require_text,
    warn_if_empty,
)
from .base import BaseAdapter, http_urls, string_list, string_or_none
from .types import AdapterError, ScannedRecord


class ReinforcementLearningAdapter(BaseAdapter):
    adapter_name = "reinforcement_learning"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prepared = False
        self._analyses: dict[str, dict[str, Any]] = {}
        self._code_map: dict[str, dict[str, Any]] = {}
        self._repositories: dict[str, dict[str, Any]] = {}

    def prepare(self, records: Sequence[ScannedRecord]) -> None:
        analyses = array_from_document(self.load_auxiliary("analyses") or [])
        code_map = array_from_document(self.load_auxiliary("code_map") or [])
        repositories = array_from_document(self.load_auxiliary("repositories") or [])
        self._analyses = mapping_index(analyses, "id")
        self._code_map = mapping_index(code_map, "id")
        self._repositories = {}
        for repo in repositories:
            if not isinstance(repo, Mapping):
                continue
            for candidate in (repo.get("canonical_url"), repo.get("url")):
                url = string_or_none(candidate)
                if url:
                    self._repositories[url.rstrip("/")] = dict(repo)
        self._prepared = True

    def _ensure_prepared(self) -> None:
        if not self._prepared:
            self.prepare(self.scan())

    def _safe_repository_local_path(
        self,
        raw_path: Any,
        warnings: list[AdapterWarning],
        *,
        field: str,
    ) -> str | None:
        """Relocate a known stale absolute path without trusting its parents.

        The legacy repository catalog predates the current ``核心领域`` parent
        directory.  If its absolute path no longer belongs to the declared
        repository root, only the final directory name is considered, and only
        when that exact child already exists under the configured root.
        """

        text = string_or_none(raw_path)
        if text is None:
            return None
        try:
            self.path_ref(text, root_key="repository")
            return text
        except AdapterError:
            basename = Path(text).name
            if basename in {"", ".", ".."}:
                return text
            declared_root = self._declared_root("repository")
            relocated = declared_root / basename
            if not relocated.is_dir():
                return text
            warnings.append(
                self.warning(
                    "stale_repository_path_relocated",
                    (
                        "A stale absolute repository path was safely relocated "
                        "to the declared repository root"
                    ),
                    severity=WarningSeverity.INFO,
                    field=field,
                    details={
                        "original_path": text,
                        "resolved_basename": basename,
                        "root_key": "repository",
                    },
                )
            )
            return basename

    def normalize(self, record: ScannedRecord) -> NormalizedRecord:
        self._ensure_prepared()
        raw = record.raw
        warnings: list[AdapterWarning] = []
        record_id = require_text(self, record, raw.get("id"), field="id")
        title = require_text(self, record, raw.get("title"), field="title")
        analysis = self._analyses.get(record_id, {})
        code_map = self._code_map.get(record_id, {})
        authors = string_list(raw.get("authors"))
        warn_if_empty(
            self,
            warnings,
            authors,
            code="missing_authors",
            message="The reinforcement-learning manifest does not provide authors",
            field="authors",
        )
        year = optional_year(self, raw.get("year"), warnings)
        source_urls = http_urls([raw.get("source_url"), code_map.get("paper_source_url")])
        asset = self.asset(
            source_record_id=record_id,
            title=title,
            authors=authors,
            year=year,
            summary=string_or_none(analysis.get("one_sentence")),
            analysis={
                key: analysis.get(key)
                for key in (
                    "problem",
                    "core_ideas",
                    "method_steps",
                    "key_concepts",
                    "experiments",
                    "contributions",
                    "limitations",
                    "reading_guide",
                    "takeaways",
                    "evidence",
                    "confidence",
                    "lineage",
                )
                if analysis.get(key) not in (None, "", [])
            },
            tags=string_list(raw.get("tags") or analysis.get("tags")),
            urls=source_urls,
            attributes={
                "section": raw.get("section"),
                "analysis_confidence": raw.get("analysis_confidence"),
                "code_status": raw.get("code_status"),
                "no_code_reason": code_map.get("no_code_reason"),
            },
        )

        pdf_path = raw.get("pdf") or code_map.get("pdf_relative_path")
        representations = [
            self.representation(
                RepresentationType.PDF,
                warnings=warnings,
                role=RepresentationRole.PRIMARY,
                raw_path=pdf_path,
                root_key="pdf",
                sha256=string_or_none(raw.get("pdf_sha256")),
                mime_type="application/pdf",
                field="pdf",
            ),
            self.representation(
                RepresentationType.HTML,
                warnings=warnings,
                role=RepresentationRole.DERIVED,
                raw_path=raw.get("paper_page") or f"papers/{int(record_id):03d}.html",
                root_key="html",
                mime_type="text/html",
                field="paper_page",
            ),
            self.representation(
                RepresentationType.ANALYSIS,
                warnings=warnings,
                role=RepresentationRole.ANALYSIS,
                raw_path=self.config.auxiliary.get("analyses"),
                metadata_value={"locator": f"[id={record_id}]"},
                field="analyses",
            ),
        ]

        repository_entries = code_map.get("repositories") or raw.get("repositories") or []
        relations = []
        for index, repository in enumerate(repository_entries):
            if not isinstance(repository, Mapping):
                continue
            repo_urls = http_urls([repository.get("url")])
            if not repo_urls:
                continue
            repo_url = repo_urls[0]
            repository_metadata = self._repositories.get(repo_url.rstrip("/"), {})
            repository_path = self._safe_repository_local_path(
                repository_metadata.get("local_path"),
                warnings,
                field=f"repositories.{index}.local_path",
            )
            representations.append(
                self.representation(
                    RepresentationType.REPOSITORY,
                    warnings=warnings,
                    role=RepresentationRole.OFFICIAL,
                    raw_path=repository_path,
                    root_key="repository",
                    url=repo_url,
                    network_required=not bool(repository_path),
                    field=f"repositories.{index}.url",
                    metadata_value={
                        key: repository_metadata.get(key)
                        for key in (
                            "commit",
                            "branch",
                            "license_spdx",
                            "status",
                            "stars",
                            "source_file_count",
                            "code_lines",
                        )
                        if repository_metadata.get(key) not in (None, "")
                    }
                    | {
                        "relation": repository.get("relation"),
                        "subdir": repository.get("subdir"),
                    },
                )
            )
            relations.append(
                self.relation(
                    RelationType.IMPLEMENTED_BY,
                    object_ref=self.repository_reference(repo_url),
                    field=f"repositories.{index}.url",
                    excerpt=string_or_none(repository.get("evidence")) or repo_url,
                    confidence=1.0,
                    confirmed=True,
                    metadata_value={
                        "relation": repository.get("relation"),
                        "note": repository.get("note"),
                    },
                )
            )
            folder = repository_folder(repo_url)
            if folder:
                guide_path = f"{folder}/guide.html"
                if self.path_exists(guide_path, root_key="annotation"):
                    representations.append(
                        self.representation(
                            RepresentationType.GUIDE,
                            warnings=warnings,
                            role=RepresentationRole.GUIDE,
                            raw_path=guide_path,
                            root_key="annotation",
                            mime_type="text/html",
                            field=f"repositories.{index}.url",
                        )
                    )

        lineage = analysis.get("lineage")
        if isinstance(lineage, Mapping):
            for field, relation_type in (
                ("predecessors", RelationType.FOLLOWS),
                ("successors", RelationType.PRECEDES),
            ):
                for index, label in enumerate(lineage.get(field) or []):
                    text = string_or_none(label)
                    if text is None:
                        continue
                    relations.append(
                        self.relation(
                            relation_type,
                            object_ref=self.source_label_reference(text),
                            field=f"analyses.lineage.{field}.{index}",
                            excerpt=text,
                            confidence=0.4,
                        )
                    )
            if lineage.get("predecessors") or lineage.get("successors"):
                warnings.append(
                    self.warning(
                        "narrative_lineage_unresolved",
                        "Lineage labels are preserved as candidates pending entity resolution",
                        field="analyses.lineage",
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
                "repositories": self.config.auxiliary.get("repositories"),
            },
        )
