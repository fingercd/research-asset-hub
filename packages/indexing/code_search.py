"""Small symbol-aware facade over a keyword index."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .meili import KeywordIndex
from .models import SearchHit


class CodeSearch:
    def __init__(self, index: KeywordIndex) -> None:
        self.index = index

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        language: str | None = None,
    ) -> list[SearchHit]:
        filters: Mapping[str, Any] = (
            {"kind": "code", "language": language} if language else {"kind": "code"}
        )
        return self.index.search(query, limit=limit, filters=filters)
