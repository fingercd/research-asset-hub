"""Meilisearch abstraction with deterministic in-memory fallback."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx

from .filters import matches_metadata, meilisearch_filter
from .models import (
    BackendHealth,
    BackendState,
    SearchDocument,
    SearchHit,
)

_TOKEN = re.compile(r"[a-z0-9_.:/+-]+|[\u3400-\u9fff]", re.IGNORECASE)
_AUTHORITY_IDENTIFIER = re.compile(
    r"(?:10\.\d{4,9}/\S+|\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7})",
    re.IGNORECASE,
)
MAX_KEYWORD_CODE_TEXT_CHARS = 65_536


def _chunks(values: Sequence[int], size: int) -> list[Sequence[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def tokenize(value: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(value)]


def _matches_filters(document: SearchDocument, filters: Mapping[str, Any]) -> bool:
    return matches_metadata(document.metadata, filters)


class KeywordIndex(Protocol):
    @property
    def health(self) -> BackendHealth: ...

    def upsert(self, documents: Sequence[SearchDocument]) -> None: ...

    def delete(self, document_ids: Sequence[str]) -> None: ...

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]: ...


class JSONTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class HttpxJSONTransport:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 5.0,
        wait_for_tasks: bool = True,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)
        self.wait_for_tasks = wait_for_tasks
        self._pending_task_uids: list[int] = []

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body: Any = payload
        if path.endswith("/documents") and isinstance((payload or {}).get("documents"), list):
            body = (payload or {})["documents"]
        elif path.endswith("/documents/delete-batch") and isinstance(
            (payload or {}).get("ids"), list
        ):
            body = (payload or {})["ids"]
        response = self.client.request(method, path, json=body)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise TypeError("Expected a JSON object response")
        if value.get("taskUid") is not None:
            task_uid = int(value["taskUid"])
            if self.wait_for_tasks:
                self._wait_for_task(task_uid)
            else:
                self._pending_task_uids.append(task_uid)
        return value

    def _wait_for_task(self, task_uid: int) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            response = self.client.get(f"/tasks/{task_uid}")
            response.raise_for_status()
            task = response.json()
            if not isinstance(task, Mapping):
                raise TypeError("Expected a JSON object task response")
            status = task.get("status")
            if status == "succeeded":
                return
            if status in {"failed", "canceled"}:
                error = task.get("error")
                raise RuntimeError(f"Remote task {task_uid} ended with {status}: {error}")
            time.sleep(0.05)
        raise TimeoutError(f"Remote task {task_uid} did not finish within 60 seconds")

    def flush_pending_tasks(
        self,
        *,
        timeout_seconds: float = 3600,
        poll_seconds: float = 0.1,
    ) -> None:
        """Wait for queued Meilisearch writes and verify every task outcome.

        Enqueuing compatible document additions before waiting lets
        Meilisearch's scheduler batch them into a much smaller number of index
        commits. Each original task still receives an independently verified
        terminal status.
        """

        pending = set(self._pending_task_uids)
        if not pending:
            return
        deadline = time.monotonic() + timeout_seconds
        while pending and time.monotonic() < deadline:
            for uid_batch in _chunks(sorted(pending), 100):
                response = self.client.get(
                    "/tasks",
                    params={
                        "uids": ",".join(str(uid) for uid in uid_batch),
                        "limit": len(uid_batch),
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise TypeError("Expected a JSON object task-list response")
                results = payload.get("results", [])
                if not isinstance(results, Sequence):
                    raise TypeError("Expected a JSON task result sequence")
                for task in results:
                    if not isinstance(task, Mapping) or task.get("uid") is None:
                        continue
                    uid = int(task["uid"])
                    status = task.get("status")
                    if status == "succeeded":
                        pending.discard(uid)
                    elif status in {"failed", "canceled"}:
                        self._pending_task_uids.clear()
                        raise RuntimeError(
                            f"Remote task {uid} ended with {status}: {task.get('error')}"
                        )
            if pending:
                time.sleep(poll_seconds)
        if pending:
            raise TimeoutError(
                f"{len(pending)} queued Meilisearch tasks did not finish within "
                f"{timeout_seconds:g} seconds"
            )
        self._pending_task_uids.clear()


class InMemoryKeywordIndex:
    def __init__(self, *, exact_boost: float = 3.0) -> None:
        self.documents: dict[str, SearchDocument] = {}
        self.exact_boost = exact_boost

    @property
    def health(self) -> BackendHealth:
        return BackendHealth("memory-keyword", BackendState.HEALTHY)

    def upsert(self, documents: Sequence[SearchDocument]) -> None:
        self.documents.update({document.document_id: document for document in documents})

    def delete(self, document_ids: Sequence[str]) -> None:
        for document_id in document_ids:
            self.documents.pop(document_id, None)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        normalized_query = query.strip().casefold()
        query_terms = Counter(tokenize(query))
        scored: list[tuple[float, bool, SearchDocument]] = []
        for document in self.documents.values():
            if filters and not _matches_filters(document, filters):
                continue
            searchable = f"{document.title}\n{document.text}"
            terms = Counter(tokenize(searchable))
            overlap = sum(min(count, terms.get(term, 0)) for term, count in query_terms.items())
            authority_exact = bool(normalized_query) and any(
                normalized_query == term.casefold() for term in document.exact_terms
            )
            phrase_exact = bool(normalized_query) and (
                normalized_query in document.title.casefold()
                or normalized_query in document.text.casefold()
            )
            exact = authority_exact or phrase_exact
            score = float(overlap)
            if phrase_exact:
                score += self.exact_boost
            if authority_exact:
                score += self.exact_boost * 2
            if score > 0:
                scored.append((score, exact, document))
        scored.sort(key=lambda item: (-item[0], item[2].document_id))
        return [
            SearchHit(
                document=document,
                score=score,
                rank=index,
                source="memory-keyword",
                exact_match=exact,
            )
            for index, (score, exact, document) in enumerate(
                scored[:limit],
                start=1,
            )
        ]


class MeilisearchIndex:
    def __init__(
        self,
        transport: JSONTransport,
        *,
        index_name: str = "documents",
    ) -> None:
        self.transport = transport
        self.index_name = index_name

    @property
    def health(self) -> BackendHealth:
        try:
            self.transport.request("GET", "/health")
        except Exception as exc:
            return BackendHealth("meilisearch", BackendState.UNAVAILABLE, str(exc))
        return BackendHealth("meilisearch", BackendState.HEALTHY)

    def upsert(self, documents: Sequence[SearchDocument]) -> None:
        payload = {
            "documents": [
                {
                    "document_id": _meili_primary_key(item.document_id),
                    "logical_document_id": item.document_id,
                    "asset_id": item.asset_id,
                    "kind": item.kind,
                    "title": item.title,
                    "text": (
                        item.text[:MAX_KEYWORD_CODE_TEXT_CHARS]
                        if item.kind == "code"
                        else item.text
                    ),
                    "metadata": item.metadata,
                    "exact_terms": list(item.exact_terms),
                }
                for item in documents
            ]
        }
        self.transport.request(
            "POST",
            f"/indexes/{self.index_name}/documents",
            payload=payload,
        )

    def delete(self, document_ids: Sequence[str]) -> None:
        self.transport.request(
            "POST",
            f"/indexes/{self.index_name}/documents/delete-batch",
            payload={"ids": [_meili_primary_key(item) for item in document_ids]},
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        payload: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "showRankingScore": True,
            "attributesToRetrieve": [
                "document_id",
                "logical_document_id",
                "asset_id",
                "kind",
                "title",
                "metadata",
                "exact_terms",
            ],
            "attributesToCrop": ["text"],
            "cropLength": 360,
            "cropMarker": "",
        }
        filter_expression = meilisearch_filter(filters)
        if filter_expression:
            payload["filter"] = filter_expression
        response: Mapping[str, Any]
        normalized_query = query.strip()
        if _AUTHORITY_IDENTIFIER.fullmatch(normalized_query):
            exact_payload = dict(payload)
            exact_payload["q"] = ""
            exact_payload["filter"] = [
                *filter_expression,
                f"exact_terms = {json.dumps(normalized_query, ensure_ascii=False)}",
            ]
            exact_response = self.transport.request(
                "POST",
                f"/indexes/{self.index_name}/search",
                payload=exact_payload,
            )
            response = (
                exact_response
                if isinstance(exact_response.get("hits"), Sequence)
                and bool(exact_response["hits"])
                else self.transport.request(
                    "POST",
                    f"/indexes/{self.index_name}/search",
                    payload=payload,
                )
            )
        else:
            response = self.transport.request(
                "POST",
                f"/indexes/{self.index_name}/search",
                payload=payload,
            )
        raw_hits = response.get("hits", [])
        if not isinstance(raw_hits, Sequence):
            return []
        hits: list[SearchHit] = []
        for rank, raw in enumerate(raw_hits, start=1):
            if not isinstance(raw, Mapping):
                continue
            formatted = raw.get("_formatted")
            cropped_text = (
                str(formatted.get("text", ""))
                if isinstance(formatted, Mapping)
                else ""
            )
            document = SearchDocument(
                document_id=str(raw.get("logical_document_id", raw.get("document_id", ""))),
                asset_id=str(raw.get("asset_id", "")),
                kind=str(raw.get("kind", "document")),
                title=str(raw.get("title", "")),
                text=cropped_text,
                metadata=(
                    dict(raw["metadata"]) if isinstance(raw.get("metadata"), Mapping) else {}
                ),
                exact_terms=tuple(str(item) for item in raw.get("exact_terms", []) or []),
            )
            normalized_query = query.strip().casefold()
            exact = bool(normalized_query) and (
                normalized_query == document.title.strip().casefold()
                or any(
                    normalized_query == exact_term.strip().casefold()
                    for exact_term in document.exact_terms
                )
            )
            hits.append(
                SearchHit(
                    document=document,
                    score=float(raw.get("_rankingScore", 0.0)),
                    rank=rank,
                    source="meilisearch",
                    exact_match=exact,
                )
            )
        return hits


class MeilisearchAdmin:
    """Create and configure indexes without depending on a Python SDK."""

    FILTERABLE_ATTRIBUTES = (
        "asset_id",
        "kind",
        "exact_terms",
        "metadata.domains",
        "metadata.year",
        "metadata.asset_type",
        "metadata.reading_status",
        "metadata.tags",
        "metadata.has_pdf",
        "metadata.has_html",
        "metadata.has_code",
        "metadata.has_guide",
        "metadata.representation_types",
        "metadata.available_representation_types",
        "metadata.availability",
    )
    SORTABLE_ATTRIBUTES = ("metadata.year", "title")
    SEARCHABLE_ATTRIBUTES = ("exact_terms", "title", "text")

    def __init__(self, transport: HttpxJSONTransport) -> None:
        self.transport = transport

    def ensure_index(self, index_name: str) -> None:
        response = self.transport.client.get(f"/indexes/{index_name}")
        if response.status_code == 404:
            self.transport.request(
                "POST",
                "/indexes",
                payload={"uid": index_name, "primaryKey": "document_id"},
            )
        else:
            response.raise_for_status()
        self.transport.request(
            "PATCH",
            f"/indexes/{index_name}/settings",
            payload={
                "filterableAttributes": list(self.FILTERABLE_ATTRIBUTES),
                "sortableAttributes": list(self.SORTABLE_ATTRIBUTES),
                "searchableAttributes": list(self.SEARCHABLE_ATTRIBUTES),
                "displayedAttributes": [
                    "document_id",
                    "logical_document_id",
                    "asset_id",
                    "kind",
                    "title",
                    "text",
                    "metadata",
                    "exact_terms",
                ],
                "typoTolerance": {
                    "disableOnAttributes": ["exact_terms"],
                },
            },
        )

    def document_count(self, index_name: str) -> int:
        response = self.transport.request("GET", f"/indexes/{index_name}/stats")
        return int(response.get("numberOfDocuments", 0))

    def document_ids(self, index_name: str) -> set[str]:
        values: set[str] = set()
        offset = 0
        while True:
            response = self.transport.request(
                "GET",
                f"/indexes/{index_name}/documents?fields=logical_document_id"
                f"&limit=1000&offset={offset}",
            )
            results = response.get("results")
            if not isinstance(results, Sequence):
                break
            for item in results:
                if isinstance(item, Mapping) and item.get("logical_document_id") is not None:
                    values.add(str(item["logical_document_id"]))
            if len(results) < 1000:
                break
            offset += len(results)
        return values


def _meili_primary_key(document_id: str) -> str:
    return hashlib.sha256(document_id.encode("utf-8")).hexdigest()


class ResilientKeywordIndex:
    """Mirror writes to memory and transparently search it on service failure."""

    def __init__(
        self,
        primary: KeywordIndex,
        fallback: InMemoryKeywordIndex | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or InMemoryKeywordIndex()
        self.last_error: str | None = None

    @property
    def health(self) -> BackendHealth:
        primary_health = self.primary.health
        if primary_health.state == BackendState.HEALTHY and self.last_error is None:
            return primary_health
        return BackendHealth(
            "keyword-resilient",
            BackendState.DEGRADED,
            self.last_error or primary_health.detail,
        )

    def upsert(self, documents: Sequence[SearchDocument]) -> None:
        self.fallback.upsert(documents)
        try:
            self.primary.upsert(documents)
        except Exception as exc:
            self.last_error = str(exc)

    def delete(self, document_ids: Sequence[str]) -> None:
        self.fallback.delete(document_ids)
        try:
            self.primary.delete(document_ids)
        except Exception as exc:
            self.last_error = str(exc)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        try:
            hits = self.primary.search(query, limit=limit, filters=filters)
        except Exception as exc:
            self.last_error = str(exc)
            return self.fallback.search(query, limit=limit, filters=filters)
        self.last_error = None
        return hits
