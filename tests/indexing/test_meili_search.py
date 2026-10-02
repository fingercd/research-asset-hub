from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.indexing.meili import MeilisearchIndex
from packages.indexing.models import SearchDocument


class _SearchTransport:
    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        assert method == "POST"
        assert path == "/indexes/assets/search"
        assert payload is not None
        self.payloads.append(payload)
        return {
            "hits": [
                {
                    "document_id": "primary",
                    "logical_document_id": "asset:one",
                    "asset_id": "one",
                    "kind": "asset",
                    "title": "Auto-Encoding Variational Bayes",
                    "metadata": {},
                    "exact_terms": ["1312.6114"],
                    "_formatted": {"text": "bounded preview"},
                    "_rankingScore": 1.0,
                }
            ]
        }


class _WriteTransport:
    def __init__(self) -> None:
        self.payload: Mapping[str, Any] | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        assert method == "POST"
        assert path == "/indexes/code/documents"
        self.payload = payload
        return {"taskUid": 1}


def test_meili_search_returns_cropped_text_without_raw_text_payload() -> None:
    transport = _SearchTransport()
    index = MeilisearchIndex(transport, index_name="assets")

    hits = index.search("variational", limit=10)

    assert hits[0].document.text == "bounded preview"
    assert "text" not in transport.payloads[0]["attributesToRetrieve"]
    assert transport.payloads[0]["attributesToCrop"] == ["text"]


def test_meili_search_uses_exact_term_filter_for_arxiv_identifier() -> None:
    transport = _SearchTransport()
    index = MeilisearchIndex(transport, index_name="assets")

    hits = index.search("1312.6114", limit=10)

    assert hits[0].exact_match is True
    assert transport.payloads[0]["q"] == ""
    assert transport.payloads[0]["filter"] == ['exact_terms = "1312.6114"']


def test_meili_search_short_circuits_empty_asset_id_filter() -> None:
    transport = _SearchTransport()
    index = MeilisearchIndex(transport, index_name="assets")

    assert index.search("variational", filters={"asset_id": []}) == []
    assert transport.payloads == []


def test_meili_upsert_bounds_large_code_text() -> None:
    transport = _WriteTransport()
    index = MeilisearchIndex(transport, index_name="code")

    index.upsert(
        [
            SearchDocument(
                document_id="code:one",
                asset_id="one",
                kind="code",
                title="Large code",
                text="x" * 100_000,
            )
        ]
    )

    assert transport.payload is not None
    documents = transport.payload["documents"]
    assert len(documents[0]["text"]) == 65_536
