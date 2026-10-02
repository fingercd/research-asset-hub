"""Read-only knowledge queries over the operational database and rebuildable cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
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
from packages.indexing.keyword import KeywordSearchService
from packages.indexing.models import SearchHit

from .documents import document_from_chunks, page_evidence_id, text_hash
from .query_terms import query_variants

MAX_CHARS = 16_000
MAX_RESULTS = 20
MAX_CACHE_BYTES = 32 * 1024 * 1024


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
        keyword_search: Callable[[], KeywordSearchService] | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.cache_root = cache_root
        self.asset_loader = asset_loader
        self.hidden_asset = hidden_asset or self._hidden
        self.keyword_search = keyword_search

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
            "domains": asset.domain_slugs,
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
    ) -> dict[str, Any]:
        if not 1 <= limit <= MAX_RESULTS:
            raise KnowledgeError("invalid_limit", "limit must be between 1 and 20.", 422)
        if source_type not in {None, "original", "analysis"}:
            raise KnowledgeError("invalid_source_type", "Unknown evidence source type.", 422)
        query = query.strip()
        errors: list[str] = []
        if query and self.keyword_search:
            try:
                runtime = self.keyword_search()
                # Annotations are user state, and the code index has its own safe browser.
                search = KeywordSearchService(
                    {
                        name: index
                        for name, index in runtime.keyword_indexes.items()
                        if name in {"assets", "documents"}
                    }
                )
                filters = {
                    key: value
                    for key, value in {"domain": domain, "type": asset_type}.items()
                    if value is not None
                }
                variants = query_variants(query)
                # Long CJK questions make the keyword engine expand many common
                # characters. Prefer bounded scientific terms, preserving the
                # original question and making the transformation explicit.
                searched_queries = [variants[-1]]
                results = [
                    search.search(
                        searched_queries[0], filters=filters, limit=100, candidate_limit=100
                    )
                ]
                if not results[0].hits and len(variants) > 1:
                    searched_queries.append(query)
                    results = [
                        search.search(query, filters=filters, limit=100, candidate_limit=100)
                    ]
                if all(not result.degraded for result in results):
                    payload = self._indexed_results(
                        query,
                        results[0].hits,
                        domain=domain,
                        asset_type=asset_type,
                        source_type=source_type,
                        limit=limit,
                    )
                    payload["query_variants"] = variants
                    payload["searched_queries"] = searched_queries
                    payload["query_expansion"] = (
                        "bilingual_terminology" if len(variants) > 1 else None
                    )
                    return payload
                for result in results:
                    errors.extend(result.errors or ("keyword_backend_degraded",))
            except Exception as error:
                errors.append("keyword_runtime:" + type(error).__name__)
        metadata = self._metadata_search_assets(
            query, domain=domain, asset_type=asset_type, source_type=source_type, limit=limit
        )
        metadata.update(
            degraded=bool(query),
            errors=errors or (["keyword_backend_not_configured"] if query else []),
            mode="metadata",
            total_is_lower_bound=False,
        )
        return metadata

    def _indexed_results(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        domain: str | None,
        asset_type: str | None,
        source_type: str | None,
        limit: int,
    ) -> dict[str, Any]:
        asset_ids = set()
        for hit in hits:
            try:
                asset_ids.add(UUID(hit.document.asset_id))
            except ValueError:
                continue
        assets = {
            a.id: a
            for a in self.session.scalars(
                select(Asset).where(
                    Asset.id.in_(asset_ids),
                    Asset.deleted_at.is_(None),
                )
            )
            if not self.hidden_asset(a)
            and (not domain or domain in a.domain_slugs)
            and (not asset_type or a.asset_type == asset_type)
        }
        reps = list(
            self.session.scalars(
                select(Representation)
                .join(Source)
                .where(
                    Representation.asset_id.in_(assets),
                    Representation.deleted_at.is_(None),
                    Source.enabled.is_(True),
                    active_representation_clause(),
                )
            )
        )
        by_id = {r.id: r for r in reps}
        by_asset: dict[UUID, list[Representation]] = {}
        for candidate_rep in reps:
            by_asset.setdefault(candidate_rep.asset_id, []).append(candidate_rep)
        items = []
        rejected = 0
        for hit in hits:
            try:
                asset = assets.get(UUID(hit.document.asset_id))
            except ValueError:
                continue
            if asset is None:
                rejected += 1
                continue
            if source_type and not any(
                representation_source_type(r) == source_type for r in by_asset.get(asset.id, [])
            ):
                rejected += 1
                continue
            payload = self._asset_payload(asset)
            payload.update(snippet=None, evidence_id=None, anchor=None, source_type="metadata")
            representation_id = hit.document.metadata.get("representation_id")
            if representation_id:
                try:
                    rep = by_id.get(UUID(str(representation_id)))
                except ValueError:
                    rep = None
                if (
                    rep is None
                    or rep.asset_id != asset.id
                    or (source_type and representation_source_type(rep) != source_type)
                ):
                    rejected += 1
                    continue
                chunk_evidence_id = hit.document.document_id
                if not chunk_evidence_id.startswith("chunk:"):
                    rejected += 1
                    continue
                try:
                    indexed_chunk = self.session.get(DocumentChunk, UUID(chunk_evidence_id[6:]))
                except ValueError:
                    indexed_chunk = None
                if (
                    indexed_chunk is None
                    or indexed_chunk.asset_id != asset.id
                    or indexed_chunk.representation_id != rep.id
                    or indexed_chunk.content_hash
                    != (hit.document.metadata.get("content_hash") or text_hash(hit.document.text))
                ):
                    rejected += 1
                    continue
                try:
                    self._source_path(asset, rep)
                except (KnowledgeError, RetiredRepresentationError):
                    rejected += 1
                    continue
                chunk_id = hit.document.document_id
                anchor = dict(indexed_chunk.anchor or {})
                anchor.pop("bbox", None)
                anchor["precision"] = "page" if anchor.get("page") else "document"
                text = indexed_chunk.text_content
                position = text.casefold().find(query.casefold())
                start = max(0, position - 120)
                payload.update(
                    snippet=text[start : start + 600],
                    evidence_id=chunk_id,
                    chunk_evidence_id=chunk_id,
                    anchor=anchor,
                    representation_id=str(rep.id),
                    source_type=representation_source_type(rep),
                    source_hash=rep.sha256,
                    evidence_status="indexed_excerpt_requires_read",
                    requires_read_evidence=True,
                )
            elif hit.document.kind != "asset":
                rejected += 1
                continue
            payload.update(score=hit.score, rank=hit.rank)
            items.append(payload)
            if len(items) == limit:
                break
        return {
            "items": items,
            "total": len(items),
            "limit": limit,
            "query": query,
            "backend": "meilisearch",
            "mode": "keyword",
            "degraded": False,
            "errors": [],
            "candidate_limit": 100,
            "rejected_candidates": rejected,
            "total_is_lower_bound": True,
        }

    def _metadata_search_assets(
        self,
        query: str,
        *,
        domain: str | None = None,
        asset_type: str | None = None,
        source_type: str | None = None,
        limit: int = MAX_RESULTS,
    ) -> dict[str, Any]:
        if not 1 <= limit <= MAX_RESULTS:
            raise KnowledgeError("invalid_limit", "limit must be between 1 and 20.", 422)
        if source_type not in {None, "original", "analysis"}:
            raise KnowledgeError("invalid_source_type", "Unknown evidence source type.", 422)
        hidden = func.lower(cast(Asset.attributes["system_hidden"].as_string(), String))
        statement = select(Asset).where(
            Asset.deleted_at.is_(None), or_(hidden.is_(None), hidden.not_in(["true", "1"]))
        )
        query = query.strip()
        if query:
            pattern = (
                "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            statement = statement.where(
                or_(
                    *(
                        column.ilike(pattern, escape="\\")
                        for column in (
                            Asset.title,
                            Asset.title_zh,
                            Asset.abstract,
                            Asset.summary,
                            cast(Asset.authors, String),
                            cast(Asset.tags, String),
                        )
                    ),
                    exists(
                        select(ExternalIdentifierModel.id).where(
                            ExternalIdentifierModel.asset_id == Asset.id,
                            ExternalIdentifierModel.canonical.ilike(pattern, escape="\\"),
                        )
                    ),
                )
            )
        if asset_type:
            statement = statement.where(Asset.asset_type == asset_type)
        if domain:
            encoded_domain = json.dumps(domain, ensure_ascii=False)
            pattern = (
                "%"
                + encoded_domain.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            statement = statement.where(cast(Asset.domain_slugs, String).like(pattern, escape="\\"))
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
        total = self.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        assets = self.session.scalars(
            statement.order_by(Asset.year.desc().nullslast(), Asset.title, Asset.id).limit(limit)
        )
        return {
            "items": [self._asset_payload(asset) for asset in assets],
            "total": total,
            "limit": limit,
            "query": query,
            "backend": "postgresql_metadata",
        }

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

    def status(self) -> dict[str, Any]:
        return {
            "read_only": True,
            "schema_version": 1,
            "backend": "postgresql_metadata",
            "search_backend": "meilisearch",
            "search_fallback": "postgresql_metadata",
            "cache_available": self.cache_root.is_dir(),
            "document_chunks": self.session.scalar(select(func.count(DocumentChunk.id))) or 0,
            "limits": {"search_results": MAX_RESULTS, "evidence_chars": MAX_CHARS},
            "ocr_on_read": False,
        }
