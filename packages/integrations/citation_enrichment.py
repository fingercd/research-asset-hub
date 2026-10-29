"""Conservative Crossref/OpenAlex citation enrichment.

Only a paper's DOI, arXiv identifier, or title is used in provider lookups.
Provider results are mapped back to existing local assets before a candidate
``cites`` relation is created.  PostgreSQL remains the graph source of truth;
remote responses are retained only as small, auditable evidence records.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.enums import (
    AuditAction,
    ConfirmationStatus,
    OutboxStatus,
    RelationType,
)
from packages.domain.identifiers import new_uuid7
from packages.domain.models import (
    Asset,
    AuditEvent,
    ExternalIdentifierModel,
    OutboxEvent,
    Relation,
    RelationEvidenceModel,
)

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_PREFIX = re.compile(
    r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)",
    re.IGNORECASE,
)
_ARXIV_IN_TEXT = re.compile(
    r"(?:arxiv(?:\.org/(?:abs|pdf)/|:\s*))"
    r"(?P<identifier>(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
    re.IGNORECASE,
)
_OPENALEX_WORK = re.compile(r"(?:https?://openalex\.org/)?(?P<key>W\d+)$", re.IGNORECASE)


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _DOI_PREFIX.sub("", value.strip()).strip().casefold()
    return normalized if normalized.startswith("10.") and "/" in normalized else None


def _normalize_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _ARXIV_PREFIX.sub("", value.strip())
    normalized = re.sub(r"\.pdf$", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"v\d+$", "", normalized, flags=re.IGNORECASE)
    return normalized.casefold() or None


def _extract_arxiv(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = _ARXIV_IN_TEXT.search(value)
        if match:
            return _normalize_arxiv(match.group("identifier"))
    return None


def _normalize_title(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = "".join(character if character.isalnum() else " " for character in normalized)
    compact = " ".join(words.split())
    return compact or None


def _string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_title(value: object) -> str | None:
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return next((_string(item) for item in value if _string(item)), None)
    return None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CitationQuery:
    """The complete and intentionally small provider lookup contract."""

    asset_id: UUID
    doi: str | None
    arxiv: str | None
    title: str


@dataclass(frozen=True, slots=True)
class CitationReference:
    provider_reference_id: str
    doi: str | None = None
    arxiv: str | None = None
    title: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CitationFetchResult:
    provider: str
    source_key: str | None
    references: tuple[CitationReference, ...]
    fetched_at: datetime
    source_found: bool = True


class CitationProvider(Protocol):
    name: str

    def fetch_references(self, query: CitationQuery) -> CitationFetchResult:
        """Return references asserted by this provider for one local paper."""

    def close(self) -> None:
        """Release network resources."""


class CitationProviderError(RuntimeError):
    """A sanitized provider failure that is safe to put in an operations report."""

    def __init__(self, provider: str, detail: str, *, recoverable: bool) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail
        self.recoverable = recoverable


class _NotFound(CitationProviderError):
    pass


class _RetryingProvider:
    name: str

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.Client | None,
        timeout_seconds: float,
        max_attempts: int,
        retry_backoff_seconds: float,
        sleep: Callable[[float], None],
        headers: Mapping[str, str],
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers=dict(headers),
            follow_redirects=True,
        )
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(30.0, max(0.0, float(retry_after)))
                except ValueError:
                    pass
        return min(
            30.0,
            float(self._retry_backoff_seconds * (2 ** (attempt - 1))),
        )

    def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        last_detail = "request failed"
        for attempt in range(1, self._max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError):
                last_detail = "network timeout or transport failure"
            else:
                if response.status_code == 404:
                    raise _NotFound(self.name, "work was not found", recoverable=False)
                if response.status_code < 400:
                    try:
                        payload = response.json()
                    except ValueError as error:
                        raise CitationProviderError(
                            self.name,
                            "response was not valid JSON",
                            recoverable=False,
                        ) from error
                    if not isinstance(payload, dict):
                        raise CitationProviderError(
                            self.name,
                            "response JSON was not an object",
                            recoverable=False,
                        )
                    return cast(dict[str, Any], payload)
                last_detail = f"HTTP {response.status_code}"
                if response.status_code not in {408, 425, 429} and response.status_code < 500:
                    raise CitationProviderError(
                        self.name,
                        last_detail,
                        recoverable=False,
                    )
            if attempt < self._max_attempts:
                self._sleep(self._retry_delay(response, attempt))
        raise CitationProviderError(self.name, last_detail, recoverable=True)


class CrossrefCitationProvider(_RetryingProvider):
    """Read open reference metadata from the official Crossref REST API."""

    name = "crossref"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        mailto: str | None = None,
    ) -> None:
        user_agent = "ResearchAssetHub/1.0 (citation enrichment)"
        if mailto:
            user_agent = f"{user_agent}; mailto:{mailto}"
        super().__init__(
            base_url="https://api.crossref.org",
            client=client,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            sleep=sleep,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
        )

    def fetch_references(self, query: CitationQuery) -> CitationFetchResult:
        fetched_at = datetime.now(UTC)
        try:
            work = self._find_work(query)
        except _NotFound:
            return CitationFetchResult(self.name, None, (), fetched_at, source_found=False)
        if work is None:
            return CitationFetchResult(self.name, None, (), fetched_at, source_found=False)

        source_doi = _normalize_doi(_string(work.get("DOI"))) or query.doi
        source_title = _first_title(work.get("title"))
        raw_references = work.get("reference")
        if not isinstance(raw_references, list):
            raw_references = []

        references: list[CitationReference] = []
        seen: set[tuple[str | None, str | None, str | None]] = set()
        for ordinal, raw in enumerate(raw_references):
            if not isinstance(raw, dict):
                continue
            doi = _normalize_doi(_string(raw.get("DOI")))
            title = _first_title(raw.get("article-title")) or _first_title(raw.get("series-title"))
            unstructured = _string(raw.get("unstructured"))
            arxiv = _extract_arxiv(
                unstructured,
                _string(raw.get("URL")),
                _string(raw.get("url")),
            )
            key = (doi, arxiv, _normalize_title(title))
            if key == (None, None, None) or key in seen:
                continue
            seen.add(key)
            reference_key = doi or arxiv or f"title:{_normalize_title(title)}"
            evidence = {
                "source_doi": source_doi,
                "source_title": source_title,
                "reference_ordinal": ordinal,
                "reference_doi": doi,
                "reference_arxiv": arxiv,
                "reference_title": title,
            }
            references.append(
                CitationReference(
                    provider_reference_id=str(reference_key),
                    doi=doi,
                    arxiv=arxiv,
                    title=title,
                    evidence={key: value for key, value in evidence.items() if value is not None},
                )
            )
        return CitationFetchResult(
            self.name,
            source_doi or _normalize_title(source_title),
            tuple(references),
            fetched_at,
        )

    def _find_work(self, query: CitationQuery) -> dict[str, Any] | None:
        if query.doi:
            path_identifier = quote(query.doi, safe="")
            payload = self._request_json(f"/works/{path_identifier}")
            message = payload.get("message")
            return cast(dict[str, Any], message) if isinstance(message, dict) else None

        if not query.title:
            return None
        payload = self._request_json(
            "/works",
            params={
                "query.title": query.title,
                "rows": 1,
                "select": "DOI,title,reference",
            },
        )
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        items = message.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return None
        work = cast(dict[str, Any], items[0])
        if _normalize_title(_first_title(work.get("title"))) != _normalize_title(query.title):
            return None
        return work


class OpenAlexCitationProvider(_RetryingProvider):
    """Read citation assertions from the official OpenAlex Works API."""

    name = "openalex"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            base_url="https://api.openalex.org",
            client=client,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            sleep=sleep,
            headers={
                "Accept": "application/json",
                "User-Agent": "ResearchAssetHub/1.0 (citation enrichment)",
            },
        )
        self._api_key = api_key

    def _with_auth(self, params: Mapping[str, str | int]) -> dict[str, str | int]:
        result = dict(params)
        if self._api_key:
            result["api_key"] = self._api_key
        return result

    def fetch_references(self, query: CitationQuery) -> CitationFetchResult:
        fetched_at = datetime.now(UTC)
        try:
            source = self._find_work(query)
        except _NotFound:
            return CitationFetchResult(self.name, None, (), fetched_at, source_found=False)
        if source is None:
            return CitationFetchResult(self.name, None, (), fetched_at, source_found=False)

        source_id = _openalex_key(_string(source.get("id")))
        raw_ids = source.get("referenced_works")
        reference_ids = (
            [key for item in raw_ids if (key := _openalex_key(_string(item)))]
            if isinstance(raw_ids, list)
            else []
        )
        reference_ids = list(dict.fromkeys(reference_ids))

        references: list[CitationReference] = []
        for batch in _batches(reference_ids, 100):
            payload = self._request_json(
                "/works",
                params=self._with_auth(
                    {
                        "filter": f"openalex_id:{'|'.join(batch)}",
                        "per_page": len(batch),
                        "select": "id,doi,title,display_name,ids,locations",
                    }
                ),
            )
            results = payload.get("results")
            if not isinstance(results, list):
                continue
            for raw in results:
                if not isinstance(raw, dict):
                    continue
                reference = self._parse_reference(source_id, raw)
                if reference is not None:
                    references.append(reference)
        references.sort(key=lambda item: item.provider_reference_id)
        return CitationFetchResult(
            self.name,
            source_id,
            tuple(references),
            fetched_at,
        )

    def _find_work(self, query: CitationQuery) -> dict[str, Any] | None:
        select_fields = "id,doi,title,display_name,referenced_works,locations"
        if query.doi:
            path_identifier = quote(f"doi:{query.doi}", safe=":")
            return self._request_json(
                f"/works/{path_identifier}",
                params=self._with_auth({"select": select_fields}),
            )

        if query.arxiv:
            payload = self._request_json(
                "/works",
                params=self._with_auth(
                    {
                        "filter": (
                            f"locations.landing_page_url:https://arxiv.org/abs/{query.arxiv}"
                        ),
                        "per_page": 1,
                        "select": select_fields,
                    }
                ),
            )
            results = payload.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                return cast(dict[str, Any], results[0])

        if not query.title:
            return None
        payload = self._request_json(
            "/works",
            params=self._with_auth(
                {
                    "search": query.title,
                    "per_page": 1,
                    "select": select_fields,
                }
            ),
        )
        results = payload.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        work = cast(dict[str, Any], results[0])
        returned_title = _string(work.get("title")) or _string(work.get("display_name"))
        if _normalize_title(returned_title) != _normalize_title(query.title):
            return None
        return work

    def _parse_reference(
        self,
        source_id: str | None,
        raw: Mapping[str, Any],
    ) -> CitationReference | None:
        reference_id = _openalex_key(_string(raw.get("id")))
        if reference_id is None:
            return None
        title = _string(raw.get("title")) or _string(raw.get("display_name"))
        doi = _normalize_doi(_string(raw.get("doi")))
        arxiv_candidates: list[str | None] = []
        ids = raw.get("ids")
        if isinstance(ids, dict):
            arxiv_candidates.extend(_string(value) for value in ids.values())
        locations = raw.get("locations")
        if isinstance(locations, list):
            for location in locations:
                if isinstance(location, dict):
                    arxiv_candidates.extend(
                        (
                            _string(location.get("landing_page_url")),
                            _string(location.get("pdf_url")),
                        )
                    )
        arxiv = _extract_arxiv(*arxiv_candidates)
        evidence = {
            "source_openalex_id": source_id,
            "reference_openalex_id": reference_id,
            "reference_doi": doi,
            "reference_arxiv": arxiv,
            "reference_title": title,
        }
        return CitationReference(
            provider_reference_id=reference_id,
            doi=doi,
            arxiv=arxiv,
            title=title,
            evidence={key: value for key, value in evidence.items() if value is not None},
        )


def _openalex_key(value: str | None) -> str | None:
    if not value:
        return None
    match = _OPENALEX_WORK.fullmatch(value.strip())
    return match.group("key").upper() if match else None


def _batches(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


@dataclass(frozen=True, slots=True)
class _ResolvedAsset:
    asset_id: UUID
    matched_by: str
    confidence: float


class _LocalAssetIndex:
    def __init__(
        self,
        *,
        by_doi: Mapping[str, set[UUID]],
        by_arxiv: Mapping[str, set[UUID]],
        by_title: Mapping[str, set[UUID]],
    ) -> None:
        self._by_doi = by_doi
        self._by_arxiv = by_arxiv
        self._by_title = by_title

    def resolve(self, reference: CitationReference) -> _ResolvedAsset | None:
        candidates = (
            ("doi", _normalize_doi(reference.doi), self._by_doi, 0.99),
            ("arxiv", _normalize_arxiv(reference.arxiv), self._by_arxiv, 0.98),
            ("title", _normalize_title(reference.title), self._by_title, 0.85),
        )
        for matched_by, key, index, confidence in candidates:
            if key is None:
                continue
            asset_ids = index.get(key, set())
            if len(asset_ids) == 1:
                return _ResolvedAsset(next(iter(asset_ids)), matched_by, confidence)
        return None


def _load_assets(
    session: Session,
    limit: int | None,
    asset_ids: Collection[UUID] | None,
) -> tuple[list[CitationQuery], _LocalAssetIndex]:
    assets = list(
        session.scalars(
            select(Asset)
            .where(Asset.asset_type == "paper", Asset.deleted_at.is_(None))
            .order_by(Asset.id)
        ).all()
    )
    all_asset_ids = [asset.id for asset in assets]
    identifiers_by_asset: dict[UUID, list[ExternalIdentifierModel]] = defaultdict(list)
    if all_asset_ids:
        identifiers = session.scalars(
            select(ExternalIdentifierModel).where(
                ExternalIdentifierModel.asset_id.in_(all_asset_ids),
                ExternalIdentifierModel.is_identity.is_(True),
            )
        )
        for identifier in identifiers:
            identifiers_by_asset[identifier.asset_id].append(identifier)

    by_doi: dict[str, set[UUID]] = defaultdict(set)
    by_arxiv: dict[str, set[UUID]] = defaultdict(set)
    by_title: dict[str, set[UUID]] = defaultdict(set)
    queries: list[CitationQuery] = []
    for asset in assets:
        doi: str | None = None
        arxiv: str | None = None
        for identifier in identifiers_by_asset[asset.id]:
            if identifier.scheme == "doi":
                doi = doi or _normalize_doi(identifier.canonical)
            elif identifier.scheme in {"arxiv", "arxiv-id"}:
                arxiv = arxiv or _normalize_arxiv(identifier.canonical)
        normalized_title = _normalize_title(asset.title)
        if doi:
            by_doi[doi].add(asset.id)
        if arxiv:
            by_arxiv[arxiv].add(asset.id)
        if normalized_title:
            by_title[normalized_title].add(asset.id)
        queries.append(CitationQuery(asset.id, doi, arxiv, asset.title))

    selected_asset_ids = set(asset_ids) if asset_ids is not None else None
    selected_queries = (
        queries
        if selected_asset_ids is None
        else [query for query in queries if query.asset_id in selected_asset_ids]
    )
    if limit is not None:
        selected_queries = selected_queries[: max(0, limit)]
    return selected_queries, _LocalAssetIndex(
        by_doi=by_doi,
        by_arxiv=by_arxiv,
        by_title=by_title,
    )


@dataclass(frozen=True, slots=True)
class EnrichmentFailure:
    asset_id: UUID
    provider: str
    detail: str
    recoverable: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": str(self.asset_id),
            "provider": self.provider,
            "detail": self.detail,
            "recoverable": self.recoverable,
        }


@dataclass(slots=True)
class CitationEnrichmentReport:
    dry_run: bool
    limit: int | None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    sources_considered: int = 0
    provider_calls: int = 0
    sources_not_found: int = 0
    references_seen: int = 0
    references_matched: int = 0
    references_unmatched: int = 0
    self_references_skipped: int = 0
    rejected_edges_skipped: int = 0
    deleted_edges_skipped: int = 0
    existing_edges_touched: int = 0
    candidate_edges_created: int = 0
    evidence_created: int = 0
    evidence_existing: int = 0
    failures: list[EnrichmentFailure] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "completed_with_errors" if self.failures else "completed"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "research-asset-hub.citation-enrichment/v1",
            "status": self.status,
            "dry_run": self.dry_run,
            "limit": self.limit,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "sources_considered": self.sources_considered,
            "provider_calls": self.provider_calls,
            "sources_not_found": self.sources_not_found,
            "references_seen": self.references_seen,
            "references_matched": self.references_matched,
            "references_unmatched": self.references_unmatched,
            "self_references_skipped": self.self_references_skipped,
            "rejected_edges_skipped": self.rejected_edges_skipped,
            "deleted_edges_skipped": self.deleted_edges_skipped,
            "existing_edges_touched": self.existing_edges_touched,
            "candidate_edges_created": self.candidate_edges_created,
            "evidence_created": self.evidence_created,
            "evidence_existing": self.evidence_existing,
            "failures": [failure.to_dict() for failure in self.failures],
        }


def _evidence_hash(
    *,
    provider: str,
    subject_asset_id: UUID,
    object_asset_id: UUID,
    evidence: Mapping[str, Any],
) -> str:
    payload = {
        "provider": provider,
        "subject_asset_id": str(subject_asset_id),
        "object_asset_id": str(object_asset_id),
        "evidence": evidence,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def enrich_citations(
    session: Session,
    *,
    providers: Sequence[CitationProvider],
    dry_run: bool = False,
    limit: int | None = None,
    asset_ids: Collection[UUID] | None = None,
) -> CitationEnrichmentReport:
    """Enrich the local graph while preserving rejection and idempotency semantics."""

    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")
    report = CitationEnrichmentReport(dry_run=dry_run, limit=limit)
    queries, local_index = _load_assets(session, limit, asset_ids)
    report.sources_considered = len(queries)

    relation_rows: dict[tuple[UUID, UUID], list[Relation]] = defaultdict(list)
    for existing_relation in session.scalars(
        select(Relation).where(Relation.relation_type == RelationType.CITES.value)
    ):
        relation_rows[
            (existing_relation.subject_asset_id, existing_relation.object_asset_id)
        ].append(existing_relation)

    evidence_hashes: dict[UUID, set[str]] = defaultdict(set)
    for evidence in session.scalars(select(RelationEvidenceModel)):
        evidence_hashes[evidence.relation_id].add(evidence.evidence_hash)

    planned_edge_ids: dict[tuple[UUID, UUID], UUID] = {}
    existing_edges_touched: set[tuple[UUID, UUID]] = set()
    rejected_edges: set[tuple[UUID, UUID]] = set()
    deleted_edges: set[tuple[UUID, UUID]] = set()

    for query in queries:
        for provider in providers:
            report.provider_calls += 1
            try:
                result = provider.fetch_references(query)
            except CitationProviderError as error:
                report.failures.append(
                    EnrichmentFailure(
                        query.asset_id,
                        provider.name,
                        error.detail,
                        error.recoverable,
                    )
                )
                continue
            except (httpx.HTTPError, OSError) as error:
                report.failures.append(
                    EnrichmentFailure(
                        query.asset_id,
                        provider.name,
                        f"{type(error).__name__}: transport failure",
                        True,
                    )
                )
                continue

            if not result.source_found:
                report.sources_not_found += 1
            for reference in result.references:
                report.references_seen += 1
                resolved = local_index.resolve(reference)
                if resolved is None:
                    report.references_unmatched += 1
                    continue
                if resolved.asset_id == query.asset_id:
                    report.self_references_skipped += 1
                    continue
                report.references_matched += 1
                edge_key = (query.asset_id, resolved.asset_id)
                rows = relation_rows.get(edge_key, [])
                if any(
                    row.confirmation_status == ConfirmationStatus.REJECTED.value for row in rows
                ):
                    rejected_edges.add(edge_key)
                    continue
                active_rows = [row for row in rows if row.deleted_at is None]
                if rows and not active_rows:
                    deleted_edges.add(edge_key)
                    continue

                if active_rows:
                    selected_relation = sorted(
                        active_rows,
                        key=lambda row: (
                            row.confirmation_status != ConfirmationStatus.CONFIRMED.value,
                            str(row.id),
                        ),
                    )[0]
                    relation_id = selected_relation.id
                    existing_edges_touched.add(edge_key)
                elif edge_key in planned_edge_ids:
                    relation_id = planned_edge_ids[edge_key]
                else:
                    relation_id = new_uuid7()
                    planned_edge_ids[edge_key] = relation_id
                    created_relation = Relation(
                        id=relation_id,
                        subject_asset_id=query.asset_id,
                        object_asset_id=resolved.asset_id,
                        relation_type=RelationType.CITES.value,
                        source_key="citation-enrichment",
                        evidence={"origin": "citation-enrichment"},
                        confidence=resolved.confidence,
                        confirmation_status=ConfirmationStatus.CANDIDATE.value,
                        attributes={"automated": True},
                    )
                    if not dry_run:
                        session.add(created_relation)
                        relation_rows[edge_key].append(created_relation)
                        session.add(
                            AuditEvent(
                                entity_type="relation",
                                entity_id=relation_id,
                                action=AuditAction.CREATE.value,
                                actor="citation-enrichment",
                                after={
                                    "subject_asset_id": str(query.asset_id),
                                    "object_asset_id": str(resolved.asset_id),
                                    "relation_type": RelationType.CITES.value,
                                    "confirmation_status": (ConfirmationStatus.CANDIDATE.value),
                                },
                                details={"automated": True},
                            )
                        )
                        session.add(
                            OutboxEvent(
                                aggregate_type="relation",
                                aggregate_id=relation_id,
                                event_type="relation.citation.candidate-created",
                                payload={
                                    "subject_asset_id": str(query.asset_id),
                                    "object_asset_id": str(resolved.asset_id),
                                },
                                status=OutboxStatus.PENDING.value,
                                dedupe_key=(
                                    "citation-enrichment:relation:"
                                    f"{query.asset_id}:{resolved.asset_id}"
                                ),
                            )
                        )
                    report.candidate_edges_created += 1

                evidence_payload = {
                    **dict(reference.evidence),
                    "matched_by": resolved.matched_by,
                }
                digest = _evidence_hash(
                    provider=result.provider,
                    subject_asset_id=query.asset_id,
                    object_asset_id=resolved.asset_id,
                    evidence=evidence_payload,
                )
                if digest in evidence_hashes[relation_id]:
                    report.evidence_existing += 1
                    continue
                evidence_hashes[relation_id].add(digest)
                if not dry_run:
                    evidence_id = new_uuid7()
                    session.add(
                        RelationEvidenceModel(
                            id=evidence_id,
                            relation_id=relation_id,
                            provider=result.provider,
                            source_key=(f"{result.provider}:{reference.provider_reference_id}")[
                                :255
                            ],
                            evidence=evidence_payload,
                            evidence_hash=digest,
                            confidence=resolved.confidence,
                            fetched_at=result.fetched_at,
                        )
                    )
                    session.add(
                        AuditEvent(
                            entity_type="relation_evidence",
                            entity_id=evidence_id,
                            action=AuditAction.CREATE.value,
                            actor="citation-enrichment",
                            after={
                                "relation_id": str(relation_id),
                                "provider": result.provider,
                                "evidence_hash": digest,
                                "confidence": resolved.confidence,
                            },
                            details={"automated": True},
                        )
                    )
                    session.add(
                        OutboxEvent(
                            aggregate_type="relation",
                            aggregate_id=relation_id,
                            event_type="relation.citation.evidence-added",
                            payload={
                                "relation_id": str(relation_id),
                                "evidence_id": str(evidence_id),
                                "provider": result.provider,
                                "evidence_hash": digest,
                            },
                            status=OutboxStatus.PENDING.value,
                            dedupe_key=(f"citation-enrichment:evidence:{relation_id}:{digest}"),
                        )
                    )
                report.evidence_created += 1

    report.existing_edges_touched = len(existing_edges_touched)
    report.rejected_edges_skipped = len(rejected_edges)
    report.deleted_edges_skipped = len(deleted_edges)
    report.finished_at = datetime.now(UTC)
    return report


__all__ = [
    "CitationEnrichmentReport",
    "CitationFetchResult",
    "CitationProvider",
    "CitationProviderError",
    "CitationQuery",
    "CitationReference",
    "CrossrefCitationProvider",
    "EnrichmentFailure",
    "OpenAlexCitationProvider",
    "enrich_citations",
]
