"""Core relation semantics used by graph traversal and UI filters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelationDefinition:
    relation_type: str
    inverse: str
    directed: bool


CORE_RELATIONS = {
    item.relation_type: item
    for item in (
        RelationDefinition("cites", "cited_by", True),
        RelationDefinition("cited_by", "cites", True),
        RelationDefinition("extends", "extended_by", True),
        RelationDefinition("extended_by", "extends", True),
        RelationDefinition("precedes", "follows", True),
        RelationDefinition("follows", "precedes", True),
        RelationDefinition("related_to", "related_to", False),
        RelationDefinition("implements", "implemented_by", True),
        RelationDefinition("implemented_by", "implements", True),
        RelationDefinition("uses_method", "method_used_by", True),
        RelationDefinition("method_used_by", "uses_method", True),
        RelationDefinition("uses_dataset", "dataset_used_by", True),
        RelationDefinition("dataset_used_by", "uses_dataset", True),
        RelationDefinition("same_as", "same_as", False),
        RelationDefinition("similar_to", "similar_to", False),
    )
}


def inverse_relation(relation_type: str) -> str:
    definition = CORE_RELATIONS.get(relation_type)
    return definition.inverse if definition else relation_type


def is_directed(relation_type: str) -> bool:
    definition = CORE_RELATIONS.get(relation_type)
    return definition.directed if definition else True
