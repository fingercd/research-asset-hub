"""Explainable lexical scores; scores are ordering weights, not probabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .query_terms import BROAD_CONCEPTS, QueryPlan, contains, normalize


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    return "" if value is None else str(value)


def normalize_identifier(value: str) -> str:
    value = normalize(value)
    for prefix in ("doi:", "arxiv:", "https://doi.org/", "https://arxiv.org/abs/"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix).strip()
            break
    return re.sub(r"v\d+$", "", value) if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", value) else value


def lexical_rank(
    plan: QueryPlan,
    asset_metadata: Mapping[str, Any],
    snippet: str = "",
    backend_score: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    title = " ".join(_text(asset_metadata.get(key)) for key in ("title", "title_zh"))
    # Only the registered titles and original abstract contribute lexical scores.
    # The arguments remain accepted for callers, but cached excerpts and remote
    # scores may contain old body projections and therefore cannot affect order.
    abstract = _text(asset_metadata.get("abstract"))
    identifiers = []
    for key in ("external_identifiers", "identifiers", "doi", "arxiv"):
        value = asset_metadata.get(key)
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if item:
                identifiers.append(normalize_identifier(item))
    matches: list[dict[str, Any]] = []
    score = 0.0
    matched_identifier = next(
        (
            normalize_identifier(term)
            for term in (plan.original, *plan.named_terms)
            if normalize_identifier(term) in identifiers
        ),
        None,
    )
    if matched_identifier:
        score += 2000
        matches.append({"kind": "exact_identifier", "field": "identifier", "weight": 2000})
    for key in ("title", "title_zh"):
        if normalize(_text(asset_metadata.get(key))) in {
            normalize(title) for title in (plan.original, *plan.exact_titles) if normalize(title)
        }:
            score += 2000
            matches.append({"kind": "exact_title", "field": key, "weight": 2000})
            break
    for term in plan.named_terms:
        if normalize_identifier(term) in identifiers:
            continue
        if contains(title, term):
            field, weight = "title", 500
        elif contains(abstract, term):
            field, weight = "abstract", 12
        else:
            continue
        if weight:
            score += weight
            matches.append({"kind": "named_term", "term": term, "field": field, "weight": weight})
    covered = 0
    for group in plan.concept_groups:
        for field, text, weight in (
            ("title", title, 25),
            ("abstract", abstract, 8),
        ):
            matched = next((term for term in group.terms if contains(text, term)), None)
            if matched:
                if group.name in BROAD_CONCEPTS or group.name == "proof":
                    weight = max(1, weight // 2)
                score += weight
                covered += 1
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
            (term for term in condition.terms if contains(title + " " + abstract, term)), None
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
    return score, {
        "mode": "literal_keywords",
        "matches": matches,
        "metadata_concepts_covered": covered,
        "coverage_bonus": coverage_bonus,
        "conditions": condition_matches,
        "year_weight": year_weight,
        "backend_weight": 0.0,
        "needs_evidence_verification": True,
        "score_is_probability": False,
    }
