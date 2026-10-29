"""Seed and verify deterministic, non-user recovery acceptance records.

The fixture is intentionally isolated from normal use:

* every record has a stable identifier and an explicit ``system-fixture`` marker;
* both assets, their annotation, and their manual relation are soft-deleted;
* the integration is disabled and the sync conflict is already resolved;
* outbox records are marked published, so workers never dispatch fixture work.

No source file, imported asset, user annotation, or other private content is read
or modified by this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from packages.domain.enums import (
    AuditAction,
    ConfirmationStatus,
    ConflictResolution,
    IntegrationStatus,
    OutboxStatus,
    ReadingStatus,
    RelationType,
    SyncConflictStatus,
    SyncStatus,
    TombstoneStatus,
    Visibility,
)
from packages.domain.models import (
    Annotation,
    AnnotationRevision,
    Asset,
    AuditEvent,
    Integration,
    IntegrationObject,
    OutboxEvent,
    ReadingState,
    Relation,
    SyncConflict,
    SyncRun,
    Tombstone,
)

FIXTURE_SCHEMA = "research-asset-hub.recovery-fixture/v1"
FIXTURE_KEY = "system.restore.acceptance.v1"
FIXTURE_ACTOR = "system:recovery-fixture"
FIXTURE_REQUEST_PREFIX = "system-fixture:recovery:"
FIXTURE_TIME = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
FIXTURE_NAMESPACE = UUID("d97f7184-220d-49dd-ad5e-64eadb6d7fd9")


def _stable_id(name: str) -> UUID:
    return uuid5(FIXTURE_NAMESPACE, f"{FIXTURE_KEY}:{name}")


ENTITY_IDS: dict[str, UUID] = {
    "asset_a": _stable_id("asset:a"),
    "asset_b": _stable_id("asset:b"),
    "reading_state": _stable_id("reading-state"),
    "annotation": _stable_id("annotation"),
    "annotation_revision_1": _stable_id("annotation-revision:1"),
    "annotation_revision_2": _stable_id("annotation-revision:2"),
    "relation": _stable_id("relation"),
    "integration": _stable_id("integration"),
    "integration_object": _stable_id("integration-object"),
    "sync_run": _stable_id("sync-run"),
    "sync_conflict": _stable_id("sync-conflict"),
    "tombstone": _stable_id("tombstone"),
}


@dataclass(frozen=True, slots=True)
class MutationSpec:
    key: str
    entity_type: str
    entity_id: UUID
    action: AuditAction
    before: dict[str, object] | None
    after: dict[str, object] | None

    @property
    def event_type(self) -> str:
        return f"system_fixture.{self.entity_type}.{self.action.value}"

    @property
    def request_id(self) -> str:
        return f"{FIXTURE_REQUEST_PREFIX}{self.key}"

    @property
    def audit_id(self) -> UUID:
        return _stable_id(f"audit:{self.key}")

    @property
    def outbox_id(self) -> UUID:
        return _stable_id(f"outbox:{self.key}")

    @property
    def dedupe_key(self) -> str:
        return f"system-fixture:{FIXTURE_KEY}:{self.key}"


MUTATIONS: tuple[MutationSpec, ...] = (
    MutationSpec(
        "asset-a-create",
        "asset",
        ENTITY_IDS["asset_a"],
        AuditAction.CREATE,
        None,
        {"fixture_key": FIXTURE_KEY, "title": "[SYSTEM FIXTURE] Recovery asset A"},
    ),
    MutationSpec(
        "asset-a-delete",
        "asset",
        ENTITY_IDS["asset_a"],
        AuditAction.DELETE,
        {"deleted_at": None},
        {"deleted_at": FIXTURE_TIME.isoformat(), "soft_delete": True},
    ),
    MutationSpec(
        "asset-b-create",
        "asset",
        ENTITY_IDS["asset_b"],
        AuditAction.CREATE,
        None,
        {"fixture_key": FIXTURE_KEY, "title": "[SYSTEM FIXTURE] Recovery asset B"},
    ),
    MutationSpec(
        "asset-b-delete",
        "asset",
        ENTITY_IDS["asset_b"],
        AuditAction.DELETE,
        {"deleted_at": None},
        {"deleted_at": FIXTURE_TIME.isoformat(), "soft_delete": True},
    ),
    MutationSpec(
        "reading-state-create",
        "reading_state",
        ENTITY_IDS["reading_state"],
        AuditAction.CREATE,
        None,
        {"status": ReadingStatus.READ.value, "actor": FIXTURE_ACTOR},
    ),
    MutationSpec(
        "annotation-create",
        "annotation",
        ENTITY_IDS["annotation"],
        AuditAction.CREATE,
        None,
        {"fixture_key": FIXTURE_KEY, "revision": 1},
    ),
    MutationSpec(
        "annotation-update",
        "annotation",
        ENTITY_IDS["annotation"],
        AuditAction.UPDATE,
        {"revision": 1},
        {"revision": 2, "change": "system fixture revision marker"},
    ),
    MutationSpec(
        "annotation-delete",
        "annotation",
        ENTITY_IDS["annotation"],
        AuditAction.DELETE,
        {"deleted_at": None},
        {"deleted_at": FIXTURE_TIME.isoformat(), "soft_delete": True},
    ),
    MutationSpec(
        "relation-create",
        "relation",
        ENTITY_IDS["relation"],
        AuditAction.CREATE,
        None,
        {"source_key": FIXTURE_KEY, "confirmation_status": "candidate"},
    ),
    MutationSpec(
        "relation-update",
        "relation",
        ENTITY_IDS["relation"],
        AuditAction.UPDATE,
        {"confirmation_status": "candidate", "fixture_revision": 1},
        {"confirmation_status": "confirmed", "fixture_revision": 2},
    ),
    MutationSpec(
        "relation-delete",
        "relation",
        ENTITY_IDS["relation"],
        AuditAction.DELETE,
        {"deleted_at": None},
        {"deleted_at": FIXTURE_TIME.isoformat(), "soft_delete": True},
    ),
    MutationSpec(
        "integration-create",
        "integration",
        ENTITY_IDS["integration"],
        AuditAction.CREATE,
        None,
        {"provider": "system-fixture", "enabled": False},
    ),
    MutationSpec(
        "integration-object-create",
        "integration_object",
        ENTITY_IDS["integration_object"],
        AuditAction.CREATE,
        None,
        {"external_id": FIXTURE_KEY, "remote_deleted": True},
    ),
    MutationSpec(
        "sync-run-create",
        "sync_run",
        ENTITY_IDS["sync_run"],
        AuditAction.CREATE,
        None,
        {"mode": "system-fixture", "status": SyncStatus.SUCCEEDED.value},
    ),
    MutationSpec(
        "sync-conflict-create",
        "sync_conflict",
        ENTITY_IDS["sync_conflict"],
        AuditAction.CREATE,
        None,
        {
            "field_path": "/system_fixture/value",
            "status": SyncConflictStatus.RESOLVED.value,
        },
    ),
    MutationSpec(
        "tombstone-create",
        "tombstone",
        ENTITY_IDS["tombstone"],
        AuditAction.CREATE,
        None,
        {"external_id": FIXTURE_KEY, "status": TombstoneStatus.ACTIVE.value},
    ),
)

EXPECTED_COUNTS: dict[str, int] = {
    "assets": 2,
    "reading_states": 1,
    "annotations": 1,
    "annotation_revisions": 2,
    "relations": 1,
    "integrations": 1,
    "integration_objects": 1,
    "sync_runs": 1,
    "sync_conflicts": 1,
    "tombstones": 1,
    "audit_events": len(MUTATIONS),
    "outbox_events": len(MUTATIONS),
}

T = TypeVar("T")


class FixtureSafetyError(RuntimeError):
    """Raised instead of adopting or changing a record not owned by this fixture."""


@dataclass(slots=True)
class ChangeCounter:
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)

    def mark_created(self, bucket: str) -> None:
        self.created[bucket] = self.created.get(bucket, 0) + 1

    def mark_updated(self, bucket: str) -> None:
        self.updated[bucket] = self.updated.get(bucket, 0) + 1


def _ensure_owned(
    session: Session,
    model: type[T],
    entity_id: UUID,
    factory: Callable[[], T],
    ownership_check: Callable[[T], bool],
    *,
    bucket: str,
    changes: ChangeCounter,
) -> T:
    entity = session.get(model, entity_id)
    if entity is None:
        entity = factory()
        session.add(entity)
        session.flush()
        changes.mark_created(bucket)
        return entity
    if not ownership_check(entity):
        raise FixtureSafetyError(
            f"Stable fixture ID collision in {bucket}; refusing to modify record {entity_id}."
        )
    return entity


def _asset_factory(asset_id: UUID, suffix: str) -> Asset:
    return Asset(
        id=asset_id,
        asset_type="note",
        title=f"[SYSTEM FIXTURE] Recovery asset {suffix}",
        authors=[],
        analysis={},
        visibility=Visibility.PRIVATE.value,
        source_urls=[],
        domain_slugs=[],
        tags=["system-fixture", "restore-acceptance"],
        attributes={
            "fixture_key": FIXTURE_KEY,
            "purpose": "automated recovery acceptance only",
            "contains_user_content": False,
        },
        deleted_at=FIXTURE_TIME,
    )


def _ensure_no_natural_key_collision(session: Session) -> None:
    integration = session.scalar(
        select(Integration).where(
            Integration.provider == "system-fixture",
            Integration.name == "Recovery acceptance fixture",
        )
    )
    if integration is not None and integration.id != ENTITY_IDS["integration"]:
        raise FixtureSafetyError("Integration natural key is occupied by a non-stable record.")

    relation = session.scalar(
        select(Relation).where(
            Relation.subject_asset_id == ENTITY_IDS["asset_a"],
            Relation.relation_type == RelationType.RELATED_TO.value,
            Relation.object_asset_id == ENTITY_IDS["asset_b"],
            Relation.source_key == FIXTURE_KEY,
        )
    )
    if relation is not None and relation.id != ENTITY_IDS["relation"]:
        raise FixtureSafetyError("Relation natural key is occupied by a non-stable record.")

    conflict = session.scalar(
        select(SyncConflict).where(
            SyncConflict.sync_run_id == ENTITY_IDS["sync_run"],
            SyncConflict.external_object_id == ENTITY_IDS["integration_object"],
            SyncConflict.field_path == "/system_fixture/value",
        )
    )
    if conflict is not None and conflict.id != ENTITY_IDS["sync_conflict"]:
        raise FixtureSafetyError("Sync conflict natural key is occupied by a non-stable record.")

    tombstone = session.scalar(
        select(Tombstone).where(
            Tombstone.integration_id == ENTITY_IDS["integration"],
            Tombstone.side == "external",
            Tombstone.external_type == "system-fixture-asset",
            Tombstone.external_id == FIXTURE_KEY,
        )
    )
    if tombstone is not None and tombstone.id != ENTITY_IDS["tombstone"]:
        raise FixtureSafetyError("Tombstone natural key is occupied by a non-stable record.")


def _ensure_mutation_records(
    session: Session,
    spec: MutationSpec,
    changes: ChangeCounter,
) -> None:
    existing_dedupe = session.scalar(
        select(OutboxEvent).where(OutboxEvent.dedupe_key == spec.dedupe_key)
    )
    if existing_dedupe is not None and existing_dedupe.id != spec.outbox_id:
        raise FixtureSafetyError(f"Outbox dedupe key collision for fixture event {spec.key}.")

    audit = _ensure_owned(
        session,
        AuditEvent,
        spec.audit_id,
        lambda: AuditEvent(
            id=spec.audit_id,
            entity_type=spec.entity_type,
            entity_id=spec.entity_id,
            action=spec.action.value,
            actor=FIXTURE_ACTOR,
            request_id=spec.request_id,
            before=spec.before,
            after=spec.after,
            details={
                "fixture_key": FIXTURE_KEY,
                "system_fixture": True,
                "contains_user_content": False,
            },
            occurred_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.actor == FIXTURE_ACTOR
            and row.request_id == spec.request_id
            and row.details.get("fixture_key") == FIXTURE_KEY
        ),
        bucket="audit_events",
        changes=changes,
    )
    if (
        audit.entity_type != spec.entity_type
        or audit.entity_id != spec.entity_id
        or audit.action != spec.action.value
    ):
        raise FixtureSafetyError(f"Audit fixture event {spec.key} has unexpected immutable fields.")

    outbox = _ensure_owned(
        session,
        OutboxEvent,
        spec.outbox_id,
        lambda: OutboxEvent(
            id=spec.outbox_id,
            aggregate_type=spec.entity_type,
            aggregate_id=spec.entity_id,
            event_type=spec.event_type,
            payload={
                "fixture_key": FIXTURE_KEY,
                "system_fixture": True,
                "contains_user_content": False,
                "action": spec.action.value,
                "entity_type": spec.entity_type,
                "entity_id": str(spec.entity_id),
            },
            status=OutboxStatus.PUBLISHED.value,
            attempts=0,
            available_at=FIXTURE_TIME,
            published_at=FIXTURE_TIME,
            dedupe_key=spec.dedupe_key,
        ),
        lambda row: (
            row.dedupe_key == spec.dedupe_key
            and row.payload.get("fixture_key") == FIXTURE_KEY
        ),
        bucket="outbox_events",
        changes=changes,
    )
    if (
        outbox.aggregate_type != spec.entity_type
        or outbox.aggregate_id != spec.entity_id
        or outbox.event_type != spec.event_type
    ):
        raise FixtureSafetyError(
            f"Outbox fixture event {spec.key} has unexpected immutable fields."
        )


def _set_if_changed(
    entity: object,
    attribute: str,
    value: object,
    changes: ChangeCounter,
    bucket: str,
) -> None:
    current = getattr(entity, attribute)
    if isinstance(current, datetime) and isinstance(value, datetime):
        current_utc = current.replace(tzinfo=current.tzinfo or UTC).astimezone(UTC)
        value_utc = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
        equal = current_utc == value_utc
    else:
        equal = current == value
    if not equal:
        setattr(entity, attribute, value)
        changes.mark_updated(bucket)


def seed_recovery_fixture(session: Session) -> dict[str, object]:
    """Insert or repair only fixture-owned rows, then return a verification report."""

    changes = ChangeCounter()
    _ensure_no_natural_key_collision(session)

    asset_a = _ensure_owned(
        session,
        Asset,
        ENTITY_IDS["asset_a"],
        lambda: _asset_factory(ENTITY_IDS["asset_a"], "A"),
        lambda row: row.attributes.get("fixture_key") == FIXTURE_KEY,
        bucket="assets",
        changes=changes,
    )
    asset_b = _ensure_owned(
        session,
        Asset,
        ENTITY_IDS["asset_b"],
        lambda: _asset_factory(ENTITY_IDS["asset_b"], "B"),
        lambda row: row.attributes.get("fixture_key") == FIXTURE_KEY,
        bucket="assets",
        changes=changes,
    )
    _set_if_changed(asset_a, "deleted_at", FIXTURE_TIME, changes, "assets")
    _set_if_changed(asset_b, "deleted_at", FIXTURE_TIME, changes, "assets")

    reading_state = _ensure_owned(
        session,
        ReadingState,
        ENTITY_IDS["reading_state"],
        lambda: ReadingState(
            id=ENTITY_IDS["reading_state"],
            user_id=FIXTURE_ACTOR,
            asset_id=asset_a.id,
            status=ReadingStatus.READ.value,
            started_at=FIXTURE_TIME,
            read_at=FIXTURE_TIME,
        ),
        lambda row: row.user_id == FIXTURE_ACTOR and row.asset_id == asset_a.id,
        bucket="reading_states",
        changes=changes,
    )
    _set_if_changed(
        reading_state,
        "status",
        ReadingStatus.READ.value,
        changes,
        "reading_states",
    )

    annotation = _ensure_owned(
        session,
        Annotation,
        ENTITY_IDS["annotation"],
        lambda: Annotation(
            id=ENTITY_IDS["annotation"],
            user_id=FIXTURE_ACTOR,
            asset_id=asset_a.id,
            kind="comment",
            title="[SYSTEM FIXTURE] Recovery annotation",
            summary="Deterministic non-user restore verification marker.",
            body_markdown=(
                "This is synthetic system fixture text. It contains no imported or user content."
            ),
            pinned=False,
            deleted_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.user_id == FIXTURE_ACTOR
            and row.title == "[SYSTEM FIXTURE] Recovery annotation"
            and row.asset_id == asset_a.id
        ),
        bucket="annotations",
        changes=changes,
    )
    _set_if_changed(annotation, "deleted_at", FIXTURE_TIME, changes, "annotations")

    _ensure_owned(
        session,
        AnnotationRevision,
        ENTITY_IDS["annotation_revision_1"],
        lambda: AnnotationRevision(
            id=ENTITY_IDS["annotation_revision_1"],
            annotation_id=annotation.id,
            revision=1,
            title=annotation.title,
            summary="Synthetic recovery fixture revision 1.",
            body_markdown="Synthetic system fixture revision 1; no user content.",
            anchor=None,
            editor=FIXTURE_ACTOR,
            source="system-fixture",
            created_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.annotation_id == annotation.id
            and row.revision == 1
            and row.editor == FIXTURE_ACTOR
        ),
        bucket="annotation_revisions",
        changes=changes,
    )
    _ensure_owned(
        session,
        AnnotationRevision,
        ENTITY_IDS["annotation_revision_2"],
        lambda: AnnotationRevision(
            id=ENTITY_IDS["annotation_revision_2"],
            annotation_id=annotation.id,
            revision=2,
            title=annotation.title,
            summary="Synthetic recovery fixture revision 2.",
            body_markdown="Synthetic system fixture revision 2; no user content.",
            anchor=None,
            editor=FIXTURE_ACTOR,
            source="system-fixture",
            created_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.annotation_id == annotation.id
            and row.revision == 2
            and row.editor == FIXTURE_ACTOR
        ),
        bucket="annotation_revisions",
        changes=changes,
    )

    relation = _ensure_owned(
        session,
        Relation,
        ENTITY_IDS["relation"],
        lambda: Relation(
            id=ENTITY_IDS["relation"],
            subject_asset_id=asset_a.id,
            object_asset_id=asset_b.id,
            relation_type=RelationType.RELATED_TO.value,
            source_key=FIXTURE_KEY,
            evidence={
                "kind": "system-fixture",
                "contains_user_content": False,
            },
            confidence=1.0,
            confirmation_status=ConfirmationStatus.CONFIRMED.value,
            attributes={
                "fixture_key": FIXTURE_KEY,
                "fixture_revision": 2,
                "modified": True,
            },
            deleted_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.source_key == FIXTURE_KEY
            and row.attributes.get("fixture_key") == FIXTURE_KEY
        ),
        bucket="relations",
        changes=changes,
    )
    _set_if_changed(
        relation,
        "confirmation_status",
        ConfirmationStatus.CONFIRMED.value,
        changes,
        "relations",
    )
    _set_if_changed(relation, "deleted_at", FIXTURE_TIME, changes, "relations")

    integration = _ensure_owned(
        session,
        Integration,
        ENTITY_IDS["integration"],
        lambda: Integration(
            id=ENTITY_IDS["integration"],
            provider="system-fixture",
            name="Recovery acceptance fixture",
            config={
                "fixture_key": FIXTURE_KEY,
                "contains_user_content": False,
                "network_access": False,
            },
            enabled=False,
            status=IntegrationStatus.DISABLED.value,
        ),
        lambda row: (
            row.provider == "system-fixture"
            and row.config.get("fixture_key") == FIXTURE_KEY
        ),
        bucket="integrations",
        changes=changes,
    )
    _set_if_changed(integration, "enabled", False, changes, "integrations")
    _set_if_changed(
        integration,
        "status",
        IntegrationStatus.DISABLED.value,
        changes,
        "integrations",
    )

    integration_object = _ensure_owned(
        session,
        IntegrationObject,
        ENTITY_IDS["integration_object"],
        lambda: IntegrationObject(
            id=ENTITY_IDS["integration_object"],
            integration_id=integration.id,
            library_scope="system-fixture",
            external_type="system-fixture-asset",
            external_id=FIXTURE_KEY,
            asset_id=asset_a.id,
            annotation_id=annotation.id,
            external_version="fixture-v1",
            external_hash=_stable_id("external-hash").hex,
            hub_version=1,
            external_path=None,
            remote_deleted=True,
            last_synced_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.integration_id == integration.id
            and row.library_scope == "system-fixture"
            and row.external_id == FIXTURE_KEY
        ),
        bucket="integration_objects",
        changes=changes,
    )

    sync_run = _ensure_owned(
        session,
        SyncRun,
        ENTITY_IDS["sync_run"],
        lambda: SyncRun(
            id=ENTITY_IDS["sync_run"],
            integration_id=integration.id,
            status=SyncStatus.SUCCEEDED.value,
            mode="system-fixture",
            base_cursor="fixture-base",
            remote_cursor="fixture-remote",
            preview_only=False,
            counters={"system_fixture_objects": 1, "contains_user_content": False},
            started_at=FIXTURE_TIME,
            finished_at=FIXTURE_TIME,
        ),
        lambda row: row.integration_id == integration.id and row.mode == "system-fixture",
        bucket="sync_runs",
        changes=changes,
    )

    _ensure_owned(
        session,
        SyncConflict,
        ENTITY_IDS["sync_conflict"],
        lambda: SyncConflict(
            id=ENTITY_IDS["sync_conflict"],
            sync_run_id=sync_run.id,
            external_object_id=integration_object.id,
            entity_type="asset",
            entity_id=asset_a.id,
            field_path="/system_fixture/value",
            base_value={"value": "fixture-base"},
            hub_value={"value": "fixture-hub"},
            external_value={"value": "fixture-external"},
            status=SyncConflictStatus.RESOLVED.value,
            resolution=ConflictResolution.MANUAL.value,
            resolved_value={"value": "fixture-resolved"},
            resolved_by=FIXTURE_ACTOR,
            resolved_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.sync_run_id == sync_run.id
            and row.external_object_id == integration_object.id
            and row.field_path == "/system_fixture/value"
        ),
        bucket="sync_conflicts",
        changes=changes,
    )

    _ensure_owned(
        session,
        Tombstone,
        ENTITY_IDS["tombstone"],
        lambda: Tombstone(
            id=ENTITY_IDS["tombstone"],
            integration_id=integration.id,
            entity_type="asset",
            entity_id=asset_b.id,
            external_type="system-fixture-asset",
            external_id=FIXTURE_KEY,
            side="external",
            status=TombstoneStatus.ACTIVE.value,
            reason="Synthetic system fixture for recovery acceptance.",
            observed_at=FIXTURE_TIME,
        ),
        lambda row: (
            row.integration_id == integration.id
            and row.external_id == FIXTURE_KEY
            and row.side == "external"
        ),
        bucket="tombstones",
        changes=changes,
    )

    for mutation in MUTATIONS:
        _ensure_mutation_records(session, mutation, changes)

    session.flush()
    verification = verify_recovery_fixture(session)
    verification["operation"] = "seed"
    verification["created"] = dict(sorted(changes.created.items()))
    verification["created_total"] = sum(changes.created.values())
    verification["updated"] = dict(sorted(changes.updated.items()))
    verification["updated_total"] = sum(changes.updated.values())
    return verification


def _count_ids(session: Session, model: type[T], identifiers: Sequence[UUID]) -> int:
    id_column = model.__table__.c.id  # type: ignore[attr-defined]
    count = session.scalar(
        select(func.count()).select_from(model).where(id_column.in_(identifiers))
    )
    return int(count or 0)


def verify_recovery_fixture(session: Session) -> dict[str, object]:
    """Read-only verification of fixture counts, ownership, and isolation."""

    primary_ids: dict[str, Sequence[UUID]] = {
        "assets": (ENTITY_IDS["asset_a"], ENTITY_IDS["asset_b"]),
        "reading_states": (ENTITY_IDS["reading_state"],),
        "annotations": (ENTITY_IDS["annotation"],),
        "annotation_revisions": (
            ENTITY_IDS["annotation_revision_1"],
            ENTITY_IDS["annotation_revision_2"],
        ),
        "relations": (ENTITY_IDS["relation"],),
        "integrations": (ENTITY_IDS["integration"],),
        "integration_objects": (ENTITY_IDS["integration_object"],),
        "sync_runs": (ENTITY_IDS["sync_run"],),
        "sync_conflicts": (ENTITY_IDS["sync_conflict"],),
        "tombstones": (ENTITY_IDS["tombstone"],),
    }
    models: dict[str, type[object]] = {
        "assets": Asset,
        "reading_states": ReadingState,
        "annotations": Annotation,
        "annotation_revisions": AnnotationRevision,
        "relations": Relation,
        "integrations": Integration,
        "integration_objects": IntegrationObject,
        "sync_runs": SyncRun,
        "sync_conflicts": SyncConflict,
        "tombstones": Tombstone,
    }
    counts = {
        name: _count_ids(session, models[name], identifiers)
        for name, identifiers in primary_ids.items()
    }
    counts["audit_events"] = _count_ids(
        session,
        AuditEvent,
        tuple(spec.audit_id for spec in MUTATIONS),
    )
    counts["outbox_events"] = _count_ids(
        session,
        OutboxEvent,
        tuple(spec.outbox_id for spec in MUTATIONS),
    )

    visible_assets = int(
        session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.id.in_((ENTITY_IDS["asset_a"], ENTITY_IDS["asset_b"])),
                Asset.deleted_at.is_(None),
            )
        )
        or 0
    )
    unpublished_outbox = int(
        session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.id.in_(tuple(spec.outbox_id for spec in MUTATIONS)),
                OutboxEvent.status != OutboxStatus.PUBLISHED.value,
            )
        )
        or 0
    )
    annotation = session.get(Annotation, ENTITY_IDS["annotation"])
    relation = session.get(Relation, ENTITY_IDS["relation"])
    integration = session.get(Integration, ENTITY_IDS["integration"])
    ownership_valid = bool(
        annotation is not None
        and annotation.user_id == FIXTURE_ACTOR
        and annotation.deleted_at is not None
        and relation is not None
        and relation.source_key == FIXTURE_KEY
        and relation.deleted_at is not None
        and integration is not None
        and integration.config.get("fixture_key") == FIXTURE_KEY
        and not integration.enabled
    )
    mismatches = {
        name: {"expected": expected, "actual": counts.get(name, 0)}
        for name, expected in EXPECTED_COUNTS.items()
        if counts.get(name, 0) != expected
    }
    invariants = {
        "normal_search_visible_assets": visible_assets,
        "assets_soft_deleted": visible_assets == 0 and counts["assets"] == 2,
        "annotation_soft_deleted": annotation is not None and annotation.deleted_at is not None,
        "relation_soft_deleted": relation is not None and relation.deleted_at is not None,
        "integration_disabled": integration is not None and not integration.enabled,
        "unpublished_outbox_events": unpublished_outbox,
        "ownership_valid": ownership_valid,
        "contains_user_content": False,
    }
    verified = (
        not mismatches
        and visible_assets == 0
        and unpublished_outbox == 0
        and ownership_valid
    )
    return {
        "schema": FIXTURE_SCHEMA,
        "fixture_key": FIXTURE_KEY,
        "operation": "verify",
        "status": "verified" if verified else "incomplete",
        "counts": counts,
        "expected_counts": EXPECTED_COUNTS,
        "count_mismatches": mismatches,
        "invariants": invariants,
        "stable_ids": {name: str(identifier) for name, identifier in ENTITY_IDS.items()},
        "event_ids": {
            spec.key: {
                "audit_id": str(spec.audit_id),
                "outbox_id": str(spec.outbox_id),
            }
            for spec in MUTATIONS
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="Operational database URL; defaults to HUB_DATABASE_URL or the local setting.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Read-only verification; do not seed or repair fixture records.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional JSON report path (normally under work/validation).",
    )
    return parser


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    arguments = _parser().parse_args()
    settings = (
        ApiSettings(database_url=arguments.database_url)
        if arguments.database_url
        else ApiSettings()
    )
    database = DatabaseManager(settings)
    try:
        with database.session() as session:
            report = (
                verify_recovery_fixture(session)
                if arguments.verify
                else seed_recovery_fixture(session)
            )
        if arguments.report is not None:
            _write_report(arguments.report, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "verified" else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": FIXTURE_SCHEMA,
                    "fixture_key": FIXTURE_KEY,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
