"""Explainable lexical scores; scores are ordering weights, not probabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .query_terms import BROAD_CONCEPTS, IDENTIFIER, QueryPlan, contains, normalize


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    return "" if value is None else str(value)


def lexical_rank(
    plan: QueryPlan,
    asset_metadata: Mapping[str, Any],
    snippet: str = "",
    backend_score: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    title = " ".join(_text(asset_metadata.get(key)) for key in ("title", "title_zh"))
    tags = " ".join(_text(asset_metadata.get(key)) for key in ("tags", "domains", "keywords"))
    abstract = " ".join(_text(asset_metadata.get(key)) for key in ("abstract", "summary"))
    metadata = " ".join(
        (
            title,
            tags,
            _text(asset_metadata.get("external_identifiers")),
            _text(asset_metadata.get("identifiers")),
            _text(asset_metadata.get("doi")),
            _text(asset_metadata.get("arxiv")),
            _text(asset_metadata.get("source_urls")),
        )
    )
    matches: list[dict[str, Any]] = []
    score = 0.0
    for key in ("title", "title_zh"):
        if normalize(_text(asset_metadata.get(key))) in {
            normalize(title) for title in (plan.original, *plan.exact_titles) if normalize(title)
        }:
            score += 2000
            matches.append({"kind": "exact_title", "field": key, "weight": 2000})
            break
    for term in plan.named_terms:
        exact_weight = 2000 if IDENTIFIER.fullmatch(term) else 500
        field, weight = ("metadata", exact_weight) if contains(metadata, term) else ("snippet", 12)
        if contains(metadata if field == "metadata" else snippet, term):
            score += weight
            matches.append({"kind": "named_term", "term": term, "field": field, "weight": weight})
    covered = 0
    for group in plan.concept_groups:
        for field, text, weight in (
            ("title", title, 25),
            ("tags", tags, 16),
            ("abstract", abstract, 8),
            ("snippet", snippet, 3),
        ):
            matched = next((term for term in group.terms if contains(text, term)), None)
            if matched:
                if group.name in BROAD_CONCEPTS or group.name == "proof":
                    weight = max(1, weight // 2)
                score += weight
                covered += field != "snippet"
                matches.append(
                    {
                        "kind": "concept",
                        "concept": group.name,
                        "term": matched,
                        "field": field,
                        "weight": weight,
                    }
                )
                break
    coverage_bonus = max(0, covered - 1) * 5
    score += coverage_bonus
    condition_matches = []
    for condition in plan.conditions:
        matched = next(
            (term for term in condition.terms if contains(title + " " + tags, term)), None
        )
        weight = (4 if condition.polarity == "require" else -2) if matched else 0
        score += weight
        condition_matches.append(
            {
                "text": condition.text,
                "polarity": condition.polarity,
                "matched_term": matched,
                "weight": weight,
                "needs_evidence_verification": True,
            }
        )
    year_weight = (
        4 if plan.year is not None and str(asset_metadata.get("year")) == str(plan.year) else 0
    )
    score += year_weight
    backend_weight = max(0.0, min(2.0, backend_score))
    score += backend_weight
    return score, {
        "mode": "literal_keywords",
        "matches": matches,
        "metadata_concepts_covered": covered,
        "coverage_bonus": coverage_bonus,
        "conditions": condition_matches,
        "year_weight": year_weight,
        "backend_weight": backend_weight,
        "needs_evidence_verification": True,
        "score_is_probability": False,
    }
