"""Deterministic lexical reranking for retrieved evidence."""

from __future__ import annotations

from dataclasses import replace

from packages.indexing.meili import tokenize

from .models import RetrievedEvidence


def lexical_rerank(
    query: str,
    evidence: list[RetrievedEvidence],
) -> list[RetrievedEvidence]:
    terms = set(tokenize(query))
    scored = []
    for item in evidence:
        document_terms = set(
            tokenize(f"{item.hit.document.title} {item.hit.document.text}")
        )
        overlap = len(terms & document_terms)
        scored.append((item.hit.score + overlap * 0.01, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].citation_id))
    return [
        replace(item, hit=replace(item.hit, score=score, rank=rank))
        for rank, (score, item) in enumerate(scored, start=1)
    ]
