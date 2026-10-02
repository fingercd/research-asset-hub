"""Serving-scope rules for representations owned by retired sources.

Source retirement is an immutable provenance event.  It must not soft-delete or
rewrite the historical source/representation rows, but an active (not rolled
back) retirement removes those representations from every user-facing serving
surface.  Keeping the rule here prevents the API and asset gateway from
silently disagreeing about what is active.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.orm import Session

from packages.domain.models import (
    Representation,
    SourceRetirement,
    SourceRetirementRepresentationMap,
    SourceRetirementRollback,
)


@dataclass(frozen=True, slots=True)
class RepresentationServingStatus:
    """A path-free serving decision safe to expose in problem responses."""

    active: bool
    retirement_id: UUID | None = None
    successor_representation_id: UUID | None = None


class RetiredRepresentationError(LookupError):
    """Raised when a direct serving endpoint targets a retired representation."""

    def __init__(self, status: RepresentationServingStatus) -> None:
        super().__init__("The representation belongs to a retired source.")
        self.retirement_id = status.retirement_id
        self.successor_representation_id = status.successor_representation_id


def active_source_clause(source_id_column: Any) -> Any:
    """Return a SQL predicate that admits null or non-retired source IDs."""

    active_retirement = exists(
        select(SourceRetirement.id).where(
            SourceRetirement.source_id == source_id_column,
            ~exists(
                select(SourceRetirementRollback.id).where(
                    SourceRetirementRollback.retirement_id == SourceRetirement.id,
                )
            ),
        )
    )
    return or_(source_id_column.is_(None), ~active_retirement)


def active_representation_clause() -> Any:
    """Return the canonical SQL predicate for actively served representations."""

    return active_source_clause(Representation.source_id)


def apply_active_representation_scope(statement: Select[Any]) -> Select[Any]:
    """Restrict a representation query to the active serving scope."""

    return statement.where(active_representation_clause())


def representation_serving_status(
    session: Session,
    representation: Representation,
) -> RepresentationServingStatus:
    """Resolve active/retired state and a mapped successor without exposing paths."""

    if representation.source_id is None:
        return RepresentationServingStatus(active=True)

    retirement_id = session.scalar(
        select(SourceRetirement.id)
        .where(
            SourceRetirement.source_id == representation.source_id,
            ~exists(
                select(SourceRetirementRollback.id).where(
                    SourceRetirementRollback.retirement_id == SourceRetirement.id,
                )
            ),
        )
        .order_by(SourceRetirement.applied_at.desc(), SourceRetirement.id.desc())
        .limit(1)
    )
    if retirement_id is None:
        return RepresentationServingStatus(active=True)

    successor_id = session.scalar(
        select(SourceRetirementRepresentationMap.successor_representation_id)
        .where(
            SourceRetirementRepresentationMap.retirement_id == retirement_id,
            SourceRetirementRepresentationMap.retired_representation_id
            == representation.id,
        )
        .limit(1)
    )
    return RepresentationServingStatus(
        active=False,
        retirement_id=retirement_id,
        successor_representation_id=successor_id,
    )


def assert_representation_active(
    session: Session,
    representation: Representation,
) -> None:
    """Reject direct access when the representation's source is actively retired."""

    status = representation_serving_status(session, representation)
    if not status.active:
        raise RetiredRepresentationError(status)


def retired_representation_ids(
    session: Session,
    representation_ids: set[UUID],
) -> set[UUID]:
    """Return which candidate representation IDs are outside serving scope."""

    if not representation_ids:
        return set()
    return set(
        session.scalars(
            select(Representation.id).where(
                Representation.id.in_(representation_ids),
                ~active_representation_clause(),
            )
        ).all()
    )


def active_representation_ids(
    session: Session,
    representation_ids: set[UUID],
) -> set[UUID]:
    """Return candidate IDs that still exist and are actively served."""

    if not representation_ids:
        return set()
    return set(
        session.scalars(
            select(Representation.id).where(
                Representation.id.in_(representation_ids),
                Representation.deleted_at.is_(None),
                active_representation_clause(),
            )
        ).all()
    )


def retired_source_ids(session: Session, source_ids: set[UUID]) -> set[UUID]:
    """Return active-retirement source IDs for search-document post-filtering."""

    if not source_ids:
        return set()
    return set(
        session.scalars(
            select(SourceRetirement.source_id).where(
                SourceRetirement.source_id.in_(source_ids),
                ~exists(
                    select(SourceRetirementRollback.id).where(
                        SourceRetirementRollback.retirement_id
                        == SourceRetirement.id,
                    )
                ),
            )
        ).all()
    )
