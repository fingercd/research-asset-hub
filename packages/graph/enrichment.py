"""Evidence-aware relation candidate de-duplication."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RelationCandidateRecord:
    subject_id: str
    relation_type: str
    object_id: str
    source_key: str
    confidence: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.subject_id,
            self.relation_type,
            self.object_id,
            self.source_key,
        )


def deduplicate_candidates(
    candidates: Sequence[RelationCandidateRecord],
) -> list[RelationCandidateRecord]:
    selected: dict[tuple[str, str, str, str], RelationCandidateRecord] = {}
    for candidate in candidates:
        previous = selected.get(candidate.identity)
        if previous is None or (candidate.confidence or 0.0) > (
            previous.confidence or 0.0
        ):
            selected[candidate.identity] = candidate
    return [selected[key] for key in sorted(selected)]
