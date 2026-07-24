"""Relation ontology and bounded query algorithms."""

from .enrichment import RelationCandidateRecord, deduplicate_candidates
from .ontology import CORE_RELATIONS, RelationDefinition, inverse_relation, is_directed
from .queries import (
    GraphPath,
    GraphQueries,
    InMemoryRelationStore,
    PostgresExecutor,
    PostgresRelationStore,
    RelationEdge,
    RelationStore,
)

__all__ = [
    "CORE_RELATIONS",
    "GraphPath",
    "GraphQueries",
    "InMemoryRelationStore",
    "PostgresExecutor",
    "PostgresRelationStore",
    "RelationCandidateRecord",
    "RelationDefinition",
    "RelationEdge",
    "RelationStore",
    "deduplicate_candidates",
    "inverse_relation",
    "is_directed",
]
