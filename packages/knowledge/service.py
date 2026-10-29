"""Read-only knowledge queries over the operational database and rebuildable cache."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session, load_only

from apps.api.domain_views import annotate_presentation_domains, presentation_domains
from apps.asset_gateway.registry import SourceConfigurationError, SourceRegistry, UnsafeAssetPath
from packages.domain.identifier_policy import effective_identifier_clause
from packages.domain.models import (
    Activity,
    Asset,
    DocumentChunk,
    ExternalIdentifierModel,
    Relation,
    Representation,
    Source,
)
from packages.domain.serving_scope import (
    RetiredRepresentationError,
    active_representation_clause,
    assert_representation_active,
)

from .documents import document_from_chunks, page_evidence_id, text_hash
from .domains import matches_domain, normalize_domain, source_domain_candidates
from .query_terms import contains, normalize, plan_query
from .ranking import lexical_rank
from .ranking import normalize_identifier as _identifier_value

MAX_CHARS = 16_000
MAX_RESULTS = 20
MAX_CACHE_BYTES = 32 * 1024 * 1024


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class KnowledgeError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status


def representation_source_type(rep: Representation) -> str | None:
    # Historical HTML role=original means the original local webpage, which can
    # itself be a Chinese analysis. The MVP admits only registered paper PDFs
    # as original evidence; author HTML requires a separate provenance contract.
    if rep.representation_type not in {
        "pdf",
        "html",
        "markdown",
        "extracted_text",
        "analysis",
        "guide",
        "annotation",
    }:
        return None
    if rep.representation_type == "pdf" and rep.role in {
        "primary",
        "original",
        "official",
        "mirror",
        "snapshot",
    }:
        return "original"
    return "analysis"


class KnowledgeService:
    def __init__(
        self,
        session: Session,
        registry: SourceRegistry,
        cache_root: Path,
        *,
        asset_loader: Callable[[Session, UUID], Asset] | None = None,
        hidden_asset: Callable[[Asset], bool] | None = None,
        taxonomy_path: Path | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.cache_root = cache_root
        self.asset_loader = asset_loader
        self.hidden_asset = hidden_asset or self._hidden
        self.taxonomy_path = taxonomy_path

    @staticmethod
    def _hidden(asset: Asset) -> bool:
        return str(asset.attributes.get("system_hidden", "")).casefold() == "true"

    def _asset(self, asset_id: UUID) -> Asset:
        asset = (
            self.asset_loader(self.session, asset_id)
            if self.asset_loader
            else self.session.get(Asset, asset_id)
        )
        if asset is None or asset.deleted_at is not None or self.hidden_asset(asset):
            raise KnowledgeError("asset_not_found", "The asset is not available.", 404)
        return asset

    @staticmethod
    def _asset_payload(asset: Asset) -> dict[str, Any]:
        return {
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type,
            "title": asset.title,
            "title_zh": asset.title_zh,
            "authors": asset.authors,
            "year": asset.year,
            "venue": asset.venue,
            "domains": presentation_domains(asset),
            "source_domains": asset.domain_slugs,
            "tags": asset.tags,
            "source_urls": asset.source_urls,
        }

    def search_assets(
        self,
        query: str,
        *,
        domain: str | None = None,
        asset_type: str | None = None,
        source_type: str | None = None,
        limit: int = MAX_RESULTS,
        offset: int = 0,
        keywords: Sequence[str] = (),
        constraints: Sequence[str] = (),
        venue: str | None = None,
        year: int | None = None,
    ) -> dict[str, Any]:
        """Recall literal title/abstract aliases over the complete live metadata.

        UUID pages bound database reads, while final lexical order is independent
        of page size. No remote top-k window or body index caps the candidates.
        """
        if not 1 <= limit <= MAX_RESULTS:
            raise KnowledgeError("invalid_limit", "limit must be between 1 and 20.", 422)
        if offset < 0:
            raise KnowledgeError("invalid_offset", "offset must be nonnegative.", 422)
        if source_type not in {None, "original", "analysis"}:
            raise KnowledgeError("invalid_source_type", "Unknown evidence source type.", 422)
        if venue is not None:
            venue = venue.strip()
            if not venue or len(venue) > 200:
                raise KnowledgeError(
                    "invalid_venue", "venue must contain 1 to 200 characters.", 422
                )
        if year is not None and not 1450 <= year <= 2200:
            raise KnowledgeError("invalid_year", "year must be between 1450 and 2200.", 422)
        query = query.strip()
        try:
            domain = normalize_domain(domain, self.taxonomy_path, session=self.session)
            plan = (
                plan_query(query, keywords=keywords, constraints=constraints)
                if query or keywords
                else None
            )
        except ValueError as error:
            raise KnowledgeError("invalid_query", str(error), 422) from error
        recall_terms = tuple(
            dict.fromkeys(
                normalize(term)
                for term in (
                    [query, *plan.queries, *plan.named_terms, *plan.exact_titles]
                    + [term for group in plan.concept_groups for term in group.terms]
                    if plan is not None
                    else []
                )
                if term.strip()
            )
        )
        identifier_terms = {
            _identifier_value(term)
            for term in [query, *(plan.named_terms if plan else ())]
            if term.strip()
        }
        if not keywords and re.fullmatch(r"10\.\d+/\S+|\d{4}\.\d{4,5}", _identifier_value(query)):
            recall_terms = ()
        hidden = func.lower(cast(Asset.attributes["system_hidden"].as_string(), String))
        statement = (
            select(Asset)
            .options(
                load_only(
                    Asset.id,
                    Asset.asset_type,
                    Asset.title,
                    Asset.title_zh,
                    Asset.abstract,
                    Asset.authors,
                    Asset.year,
                    Asset.venue,
                    Asset.domain_slugs,
                    Asset.tags,
                    Asset.source_urls,
                    Asset.attributes,
                )
            )
            .where(
                Asset.deleted_at.is_(None),
                or_(hidden.is_(None), hidden.not_in(["true", "1"])),
            )
        )
        if asset_type:
            statement = statement.where(Asset.asset_type == asset_type)
        if venue is not None:
            statement = statement.where(func.lower(func.trim(Asset.venue)) == venue.lower())
        if year is not None:
            statement = statement.where(Asset.year == year)
        domain_candidates = source_domain_candidates(domain)
        if domain_candidates:
            patterns = [
                "%" + _escape_like(json.dumps(item, ensure_ascii=False)) + "%"
                for item in domain_candidates
            ]
            statement = statement.where(
                or_(*[cast(Asset.domain_slugs, String).like(p, escape="\\") for p in patterns])
            )
        if source_type:
            original = and_(
                Representation.representation_type == "pdf",
                Representation.role.in_(["primary", "original", "official", "mirror", "snapshot"]),
            )
            analysis = and_(
                Representation.representation_type.in_(
                    ["pdf", "html", "markdown", "extracted_text", "analysis", "guide", "annotation"]
                ),
                ~original,
            )
            statement = statement.where(
                exists(
                    select(Representation.id)
                    .join(Source)
                    .where(
                        Representation.asset_id == Asset.id,
                        Representation.deleted_at.is_(None),
                        Source.enabled.is_(True),
                        active_representation_clause(),
                        analysis if source_type == "analysis" else original,
                    )
                )
            )
        candidates: list[tuple[float, dict[str, Any]]] = []
        cursor: UUID | None = None
        while True:
            page = statement if cursor is None else statement.where(Asset.id > cursor)
            assets = list(self.session.scalars(page.order_by(Asset.id).limit(256)))
            if not assets:
                break
            cursor = assets[-1].id
            annotate_presentation_domains(self.session, assets)
            identifiers: dict[UUID, list[str]] = {}
            if plan is not None:
                for owner, value in self.session.execute(
                    select(
                        ExternalIdentifierModel.asset_id, ExternalIdentifierModel.canonical
                    ).where(
                        ExternalIdentifierModel.asset_id.in_([asset.id for asset in assets]),
                        ExternalIdentifierModel.scheme.in_(["doi", "arxiv", "arxiv-id"]),
                        effective_identifier_clause(),
                    )
                ):
                    identifiers.setdefault(owner, []).append(_identifier_value(value))
            for asset in assets:
                if self.hidden_asset(asset) or not matches_domain(asset, domain):
                    continue
                title_abstract = "\n".join(
                    value for value in (asset.title, asset.title_zh, asset.abstract) if value
                )
                exact_identifier = bool(
                    identifier_terms.intersection(identifiers.get(asset.id, []))
                )
                if plan is not None and not (
                    exact_identifier or any(contains(title_abstract, term) for term in recall_terms)
                ):
                    continue
                payload = self._asset_payload(asset)
                payload.update(
                    abstract=asset.abstract,
                    snippet=(asset.abstract or "")[:600] or None,
                    evidence_id=None,
                    anchor=None,
                    source_type="metadata",
                )
                score = 0.0
                if plan is not None:
                    score, reasons = lexical_rank(
                        plan,
                        {**payload, "identifiers": identifiers.get(asset.id, [])},
                    )
                    payload.update(
                        keyword_score=score,
                        match_reasons=reasons,
                        constraints_need_evidence_verification=True,
                    )
                candidates.append((score, payload))
            if len(assets) < 256:
                break
        candidates.sort(key=lambda item: (-item[0], item[1]["asset_id"]))
        total = len(candidates)
        items = [item for _, item in candidates[offset : offset + limit]]
        for rank, item in enumerate(items, start=offset + 1):
            item["rank"] = rank
        has_more = offset + len(items) < total
        result = {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + len(items) if has_more else None,
            "query": query,
            "backend": "postgresql_keywords",
            "mode": "keyword" if plan else "metadata",
            "degraded": False,
            "errors": [],
            "total_is_lower_bound": False,
            "search_fields": ["title", "title_zh", "abstract"],
            "identifiers_exact": ["doi", "arxiv"],
            "recall_terms": list(recall_terms),
            "pagination_consistency": "live_metadata",
            "semantic_search": False,
            "metadata_filters": {"venue": venue, "year": year},
        }
        if plan is not None:
            result.update(
                query_variants=list(plan.queries),
                searched_queries=list(plan.queries),
                query_plan=plan.to_dict(),
                query_expansion="lexical_aliases",
            )
        return result

    def _active_representations(self, asset_id: UUID) -> list[Representation]:
        return list(
            self.session.scalars(
                select(Representation)
                .join(Source)
                .where(
                    Representation.asset_id == asset_id,
                    Representation.deleted_at.is_(None),
                    Source.enabled.is_(True),
                    active_representation_clause(),
                )
                .order_by(Representation.representation_type, Representation.id)
            )
        )

    def _source_path(self, asset: Asset, rep: Representation) -> Path:
        if rep.asset_id != asset.id or rep.deleted_at is not None:
            raise KnowledgeError(
                "representation_not_found", "The representation is not available.", 404
            )
        assert_representation_active(self.session, rep)
        # Ownership is checked independently before resolving any source path.
        owner = self._asset(rep.asset_id)
        if owner.id != asset.id:
            raise KnowledgeError(
                "representation_owner_mismatch", "Representation ownership changed."
            )
        source = self.session.get(Source, rep.source_id) if rep.source_id else None
        if source is None or not source.enabled:
            raise KnowledgeError("source_unavailable", "The registered source is unavailable.", 410)
        if rep.availability != "available" or not rep.path_ref:
            raise KnowledgeError("source_unavailable", "The local source is unavailable.", 410)
        try:
            path = self.registry.resolve(source.alias, rep.root_key, rep.path_ref)
            if not path.is_file():
                raise KnowledgeError(
                    "source_unavailable", "The registered source file is missing.", 410
                )
        except SourceConfigurationError:
            raise
        except (UnsafeAssetPath, OSError, ValueError) as error:
            raise KnowledgeError(
                "source_unavailable", "The registered source cannot be resolved safely.", 410
            ) from error
        return path

    def _source(self, asset: Asset, rep: Representation) -> str:
        path = self._source_path(asset, rep)
        try:
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError as error:
            raise KnowledgeError(
                "source_unavailable", "The registered source cannot be read.", 410
            ) from error
        if rep.sha256 and digest != rep.sha256:
            raise KnowledgeError("source_hash_mismatch", "The source fingerprint has changed.")
        return digest

    def require_representation(
        self, asset_id: UUID, representation_id: UUID
    ) -> tuple[Asset, Representation, str]:
        """The shared fail-closed gate for evidence and derived figure readers."""
        asset = self._asset(asset_id)
        rep = self.session.get(Representation, representation_id)
        if rep is None:
            raise KnowledgeError("representation_not_found", "Representation not found.", 404)
        return asset, rep, self._source(asset, rep)

    def _document(self, asset: Asset, rep: Representation, digest: str) -> dict[str, Any]:
        source_type = representation_source_type(rep)
        cache_path = self.cache_root / str(asset.id) / "document.json"
        if cache_path.is_file():
            try:
                # Derived caches must also stay within the controlled output directory.
                cache_path.resolve().relative_to(self.cache_root.resolve())
                if cache_path.stat().st_size > MAX_CACHE_BYTES:
                    raise ValueError("Oversize document cache")
                document = json.loads(cache_path.read_text(encoding="utf-8"))
                if document.get("representation_id") == str(rep.id):
                    return self._validate_document(document, asset, rep, digest)
            except (OSError, ValueError, TypeError, AttributeError) as error:
                raise KnowledgeError(
                    "invalid_cache", "The derived document cache is invalid."
                ) from error
        chunks = self.session.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.asset_id == asset.id,
                DocumentChunk.representation_id == rep.id,
            )
            .order_by(DocumentChunk.chunk_index, DocumentChunk.id)
        ).all()
        if not chunks:
            raise KnowledgeError(
                "evidence_not_ready", "No extracted evidence exists for this representation."
            )
        # Worker extraction fingerprints include the relative filename and its SHA-256.
        # Old rows without an Activity can still be verified by a registered file hash.
        activities = {chunk.activity_id for chunk in chunks if chunk.activity_id is not None}
        for activity_id in activities:
            activity = self.session.get(Activity, activity_id)
            paths = {
                (c.anchor or {}).get("source_path") for c in chunks if c.activity_id == activity_id
            }
            if (
                activity is None
                or activity.status != "succeeded"
                or activity.asset_id != asset.id
                or activity.representation_id != rep.id
                or not activity.output_digest
                or len(paths) != 1
                or None in paths
            ):
                raise KnowledgeError(
                    "unverified_extraction", "Extraction provenance cannot be verified."
                )
            expected = text_hash(str(next(iter(paths))) + digest)
            if activity.output_digest != expected:
                raise KnowledgeError(
                    "stale_extraction", "Extracted evidence does not match its live source."
                )
        if any(chunk.activity_id is None for chunk in chunks) and not rep.sha256:
            raise KnowledgeError(
                "unverified_extraction", "Extraction lacks a frozen source fingerprint."
            )
        try:
            document = document_from_chunks(
                asset_id=str(asset.id),
                representation_id=str(rep.id),
                source_hash=digest,
                title=asset.title,
                source_type=str(source_type),
                chunks=[
                    {
                        "id": str(chunk.id),
                        "chunk_index": chunk.chunk_index,
                        "text_content": chunk.text_content,
                        "anchor": chunk.anchor,
                    }
                    for chunk in chunks
                ],
            )
        except ValueError as error:
            raise KnowledgeError("ambiguous_document", str(error)) from error
        return document

    @staticmethod
    def _validate_document(
        document: Any, asset: Asset, rep: Representation, digest: str
    ) -> dict[str, Any]:
        if (
            document.get("schema_version") != 1
            or document.get("asset_id") != str(asset.id)
            or document.get("source_hash") != digest
            or document.get("source_type", "original") != representation_source_type(rep)
        ):
            raise KnowledgeError("stale_cache", "The derived cache does not match its live source.")
        pages = document.get("pages")
        if not isinstance(pages, list) or not pages:
            raise KnowledgeError("invalid_cache", "The document cache contains no evidence pages.")
        seen = set()
        normalized = []
        for item in pages:
            page = item.get("page")
            text = item.get("text")
            if (
                not isinstance(text, str)
                or (page is not None and (type(page) is not int or page < 1))
                or page in seen
                or (rep.representation_type == "pdf" and page is None)
            ):
                raise KnowledgeError("invalid_cache", "Invalid or duplicated cached evidence page.")
            seen.add(page)
            content_hash = text_hash(text)
            normalized.append(
                {
                    **item,
                    "content_hash": content_hash,
                    "evidence_id": page_evidence_id(str(rep.id), digest, page, content_hash),
                    "anchor": {"kind": "pdf", "page": page, "precision": "page"}
                    if page is not None
                    else {"kind": "document", "precision": "document"},
                }
            )
        return {
            **document,
            "source_type": representation_source_type(rep),
            "pages": sorted(normalized, key=lambda p: p["page"] or 0),
        }

    def get_paper(self, asset_id: UUID) -> dict[str, Any]:
        from .derived import DerivativeStore

        asset = self._asset(asset_id)
        annotate_presentation_domains(self.session, [asset])
        representations = []
        figures = []
        has_analysis_markdown = False
        for rep in self._active_representations(asset.id):
            source_type = representation_source_type(rep)
            payload: dict[str, Any] = {
                "representation_id": str(rep.id),
                "representation_type": rep.representation_type,
                "role": rep.role,
                "source_type": source_type,
                "readable": False,
                "quality_status": "unsupported",
                "reason": None,
            }
            if source_type:
                try:
                    document = self._document(asset, rep, self._source(asset, rep))
                except KnowledgeError as error:
                    payload.update(quality_status=error.code, reason=str(error))
                else:
                    payload.update(
                        readable=True,
                        quality_status=document.get("quality_status", "unknown"),
                        source_hash=document["source_hash"],
                        page_count=len(document["pages"]),
                    )
                    for figure in DerivativeStore(self.cache_root).list_figures(str(asset.id)):
                        if (
                            figure["representation_id"] != str(rep.id)
                            or figure["source_hash"] != document["source_hash"]
                            or source_type != "original"
                        ):
                            continue
                        figures.append(
                            {
                                key: figure.get(key)
                                for key in ("figure_id", "figure_number", "page", "quality_status")
                            }
                        )
                    if source_type == "original":
                        analysis_path = self.cache_root / str(asset.id) / "analysis.zh.md"
                        try:
                            analysis_path.resolve().relative_to(self.cache_root.resolve())
                            has_analysis_markdown = has_analysis_markdown or analysis_path.is_file()
                        except ValueError:
                            pass
            representations.append(payload)
        relations = []
        for relation in self.session.scalars(
            select(Relation)
            .where(
                or_(Relation.subject_asset_id == asset.id, Relation.object_asset_id == asset.id),
                Relation.deleted_at.is_(None),
            )
            .order_by(Relation.id)
        ):
            other_id = (
                relation.object_asset_id
                if relation.subject_asset_id == asset.id
                else relation.subject_asset_id
            )
            other = self.session.get(Asset, other_id)
            if other is None or other.deleted_at is not None or self.hidden_asset(other):
                continue
            relations.append(
                {
                    "relation_id": str(relation.id),
                    "relation_type": relation.relation_type,
                    "subject_asset_id": str(relation.subject_asset_id),
                    "object_asset_id": str(relation.object_asset_id),
                    "confirmation_status": relation.confirmation_status,
                    "confidence": relation.confidence,
                }
            )
        return {
            **self._asset_payload(asset),
            "abstract": asset.abstract,
            "representations": representations,
            "relations": relations,
            "figures": figures,
            "has_analysis_markdown": has_analysis_markdown,
            "quality_status": "readable"
            if any(r["readable"] for r in representations)
            else "evidence_not_ready",
        }

    def read_evidence(
        self,
        asset_id: UUID,
        *,
        representation_id: UUID | None = None,
        source_type: str = "original",
        page_start: int = 1,
        page_end: int | None = None,
        offset: int = 0,
        max_chars: int = MAX_CHARS,
    ) -> dict[str, Any]:
        if (
            source_type not in {"original", "analysis"}
            or page_start < 1
            or (page_end is not None and page_end < page_start)
            or offset < 0
            or not 1 <= max_chars <= MAX_CHARS
        ):
            raise KnowledgeError("invalid_range", "Invalid evidence source or reading range.", 422)
        asset = self._asset(asset_id)
        if representation_id:
            asset, rep, digest = self.require_representation(asset_id, representation_id)
            if representation_source_type(rep) != source_type:
                raise KnowledgeError(
                    "source_type_mismatch",
                    "The representation has a different evidence source type.",
                    422,
                )
            document = self._document(asset, rep, digest)
        else:
            candidates = [
                r
                for r in self._active_representations(asset.id)
                if representation_source_type(r) == source_type
            ]
            candidates.sort(
                key=lambda r: (r.representation_type != "pdf", r.role != "primary", str(r.id))
            )
            failures = []
            for rep in candidates:
                try:
                    document = self._document(asset, rep, self._source(asset, rep))
                    break
                except (KnowledgeError, RetiredRepresentationError) as error:
                    failures.append(error)
            else:
                if failures:
                    raise failures[0]
                raise KnowledgeError(
                    "evidence_not_ready", "No active representation matches this source type."
                )
        selected = [
            p
            for p in document["pages"]
            if (
                (p["page"] is None and page_start == 1 and page_end in {None, 1})
                or (
                    p["page"] is not None
                    and p["page"] >= page_start
                    and (page_end is None or p["page"] <= page_end)
                )
            )
        ]
        if not selected:
            raise KnowledgeError(
                "page_not_found", "No evidence exists in the requested page range.", 404
            )
        full_text = "\n\n".join(p["text"] for p in selected)
        if offset > len(full_text):
            raise KnowledgeError(
                "invalid_offset", "offset exceeds the selected evidence length.", 422
            )
        end = min(offset + max_chars, len(full_text))
        segments = []
        position = 0
        for page in selected:
            page_end_offset = position + len(page["text"])
            start = max(offset, position)
            stop = min(end, page_end_offset)
            if stop > start:
                segments.append(
                    {
                        "evidence_id": page["evidence_id"],
                        "page": page["page"],
                        "anchor": page["anchor"],
                        "content_hash": page["content_hash"],
                        "chunk_ids": page.get("chunk_ids", []),
                        "text": page["text"][start - position : stop - position],
                        "start_offset": start - position,
                        "end_offset": stop - position,
                    }
                )
            position = page_end_offset + 2
        return {
            "asset_id": str(asset.id),
            "representation_id": str(rep.id),
            "source_type": source_type,
            "source_hash": document["source_hash"],
            "quality_status": document.get("quality_status", "unknown"),
            "text": full_text[offset:end],
            "segments": segments,
            "page_start": page_start,
            "page_end": page_end,
            "offset": offset,
            "next_offset": end if end < len(full_text) else None,
            "has_more": end < len(full_text),
            "total_chars": len(full_text),
            "anchor_precision": "page" if all(p["page"] for p in selected) else "document",
        }
