"""Auditable, idempotent reconciliation of paper-to-repository source links."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.enums import (
    AuditAction,
    ConfirmationStatus,
    OutboxStatus,
    RelationType,
    SourceAssetRole,
)
from packages.domain.models import (
    Asset,
    AuditEvent,
    OutboxEvent,
    Relation,
    RelationEvidenceModel,
    Source,
    SourceRecord,
    SourceRecordAsset,
)

REPOSITORY_RECONCILIATION_PROVIDER = "repository-link-reconciliation"


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RepositoryBinding:
    source_alias: str
    source_record_database_id: UUID
    source_record_id: str
    source_record_version_id: UUID | None
    paper_asset_id: UUID
    repository_asset_id: UUID
    link_evidence: dict[str, Any]

    @property
    def evidence_payload(self) -> dict[str, Any]:
        return {
            "source_alias": self.source_alias,
            "source_record_id": self.source_record_id,
            "source_record_database_id": str(self.source_record_database_id),
            "paper_asset_id": str(self.paper_asset_id),
            "repository_asset_id": str(self.repository_asset_id),
            "source_record_asset_role": SourceAssetRole.REPOSITORY.value,
            "link_evidence": self.link_evidence,
        }

    @property
    def evidence_hash(self) -> str:
        return _json_digest(self.evidence_payload)


@dataclass(slots=True)
class RepositoryRelationReconciliationReport:
    apply: bool
    requested_confirmation_status: str
    source_alias: str | None
    source_records_scanned: int = 0
    bindings_found: int = 0
    relations_created: int = 0
    relations_reused: int = 0
    relations_updated: int = 0
    evidence_created: int = 0
    evidence_reused: int = 0
    rejected_relations_skipped: int = 0
    missing_or_invalid_endpoints: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = "applied" if self.apply else "preview"
        return payload


def _load_bindings(
    session: Session,
    report: RepositoryRelationReconciliationReport,
) -> list[RepositoryBinding]:
    statement = select(SourceRecord, Source).join(
        Source,
        Source.id == SourceRecord.source_id,
    )
    if report.source_alias is not None:
        statement = statement.where(Source.alias == report.source_alias)
    rows = session.execute(
        statement.order_by(Source.alias, SourceRecord.source_record_id)
    ).all()
    report.source_records_scanned = len(rows)

    bindings: list[RepositoryBinding] = []
    for source_record, source in rows:
        links = session.scalars(
            select(SourceRecordAsset)
            .where(
                SourceRecordAsset.source_record_id == source_record.id,
                SourceRecordAsset.deleted_at.is_(None),
            )
            .order_by(SourceRecordAsset.role, SourceRecordAsset.id)
        ).all()
        primary_ids = [
            link.asset_id
            for link in links
            if link.role == SourceAssetRole.PRIMARY.value
        ]
        if not primary_ids and source_record.asset_id is not None:
            primary_ids = [source_record.asset_id]
        unique_primary_ids = list(dict.fromkeys(primary_ids))
        repository_links = [
            link
            for link in links
            if link.role == SourceAssetRole.REPOSITORY.value
        ]
        if not repository_links:
            continue
        if len(unique_primary_ids) != 1:
            report.missing_or_invalid_endpoints += len(repository_links)
            continue
        paper = session.get(Asset, unique_primary_ids[0])
        if paper is None or paper.asset_type != "paper" or paper.deleted_at is not None:
            report.missing_or_invalid_endpoints += len(repository_links)
            continue
        for repository_link in repository_links:
            repository = session.get(Asset, repository_link.asset_id)
            if (
                repository is None
                or repository.asset_type != "repository"
                or repository.deleted_at is not None
                or repository.id == paper.id
            ):
                report.missing_or_invalid_endpoints += 1
                continue
            bindings.append(
                RepositoryBinding(
                    source_alias=source.alias,
                    source_record_database_id=source_record.id,
                    source_record_id=source_record.source_record_id,
                    source_record_version_id=repository_link.source_record_version_id,
                    paper_asset_id=paper.id,
                    repository_asset_id=repository.id,
                    link_evidence=dict(repository_link.evidence or {}),
                )
            )
    bindings.sort(
        key=lambda item: (
            item.source_alias,
            item.source_record_id,
            str(item.paper_asset_id),
            str(item.repository_asset_id),
        )
    )
    report.bindings_found = len(bindings)
    return bindings


def _relation_for_binding(session: Session, binding: RepositoryBinding) -> Relation | None:
    return session.scalar(
        select(Relation).where(
            Relation.subject_asset_id == binding.paper_asset_id,
            Relation.relation_type == RelationType.IMPLEMENTED_BY.value,
            Relation.object_asset_id == binding.repository_asset_id,
            Relation.source_key == "canonical",
        )
    )


def _record_relation_mutation(
    session: Session,
    *,
    relation: Relation,
    binding: RepositoryBinding,
    actor: str,
    created: bool,
    evidence_created: bool,
) -> None:
    details = {
        "provider": REPOSITORY_RECONCILIATION_PROVIDER,
        "source_alias": binding.source_alias,
        "source_record_id": binding.source_record_id,
        "evidence_hash": binding.evidence_hash,
        "relation_created": created,
        "evidence_created": evidence_created,
    }
    session.add(
        AuditEvent(
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.IMPORT.value,
            actor=actor,
            after={
                "subject_asset_id": str(relation.subject_asset_id),
                "relation_type": relation.relation_type,
                "object_asset_id": str(relation.object_asset_id),
                "confirmation_status": relation.confirmation_status,
            },
            details=details,
        )
    )
    session.add(
        OutboxEvent(
            aggregate_type="relation",
            aggregate_id=relation.id,
            event_type="relation.reconciled",
            payload={
                "relation_id": str(relation.id),
                "paper_asset_id": str(binding.paper_asset_id),
                "repository_asset_id": str(binding.repository_asset_id),
                "confirmation_status": relation.confirmation_status,
            },
            status=OutboxStatus.PENDING.value,
            dedupe_key=(
                f"repository-reconcile:{relation.id}:{relation.row_version}:"
                f"{binding.evidence_hash}"
            ),
        )
    )


def reconcile_repository_relations(
    session: Session,
    *,
    apply: bool = False,
    source_alias: str | None = None,
    confirmation_status: ConfirmationStatus = ConfirmationStatus.CANDIDATE,
    actor: str = "repository-reconciler",
) -> dict[str, Any]:
    """Preview or apply canonical ``paper --implemented_by--> repository`` edges.

    The function never commits.  Callers own the transaction boundary, and a
    preview performs no flush or mutation.  Rejected relations are immutable.
    """

    if confirmation_status == ConfirmationStatus.REJECTED:
        raise ValueError("repository reconciliation cannot create rejected relations")
    report = RepositoryRelationReconciliationReport(
        apply=apply,
        requested_confirmation_status=confirmation_status.value,
        source_alias=source_alias,
    )
    bindings = _load_bindings(session, report)
    for binding in bindings:
        relation = _relation_for_binding(session, binding)
        created = relation is None
        relation_updated = False
        if (
            relation is not None
            and relation.confirmation_status == ConfirmationStatus.REJECTED.value
        ):
            report.rejected_relations_skipped += 1
            report.actions.append(
                {
                    "source": f"{binding.source_alias}:{binding.source_record_id}",
                    "paper_asset_id": str(binding.paper_asset_id),
                    "repository_asset_id": str(binding.repository_asset_id),
                    "outcome": "rejected_relation_preserved",
                }
            )
            continue

        if relation is None:
            report.relations_created += 1
        else:
            report.relations_reused += 1
            should_confirm = (
                confirmation_status == ConfirmationStatus.CONFIRMED
                and relation.confirmation_status != ConfirmationStatus.CONFIRMED.value
            )
            relation_updated = relation.deleted_at is not None or should_confirm
            if relation_updated:
                report.relations_updated += 1

        existing_evidence = (
            None
            if relation is None
            else session.scalar(
                select(RelationEvidenceModel).where(
                    RelationEvidenceModel.relation_id == relation.id,
                    RelationEvidenceModel.evidence_hash == binding.evidence_hash,
                )
            )
        )
        evidence_created = existing_evidence is None
        if evidence_created:
            report.evidence_created += 1
        else:
            report.evidence_reused += 1

        outcome = (
            "would_create"
            if not apply and created
            else "would_update"
            if not apply and relation_updated
            else "would_add_evidence"
            if not apply and evidence_created
            else "unchanged"
            if not apply
            else "created"
            if created
            else "updated"
            if relation_updated
            else "evidence_added"
            if evidence_created
            else "unchanged"
        )
        report.actions.append(
            {
                "source": f"{binding.source_alias}:{binding.source_record_id}",
                "paper_asset_id": str(binding.paper_asset_id),
                "repository_asset_id": str(binding.repository_asset_id),
                "evidence_hash": binding.evidence_hash,
                "outcome": outcome,
            }
        )
        if not apply or (not created and not relation_updated and not evidence_created):
            continue

        if relation is None:
            relation = Relation(
                subject_asset_id=binding.paper_asset_id,
                object_asset_id=binding.repository_asset_id,
                relation_type=RelationType.IMPLEMENTED_BY.value,
                source_key="canonical",
                evidence={},
                confidence=1.0,
                confirmation_status=confirmation_status.value,
                attributes={
                    "reconciled_by": REPOSITORY_RECONCILIATION_PROVIDER,
                },
            )
            session.add(relation)
            session.flush()
        else:
            relation.deleted_at = None
            if confirmation_status == ConfirmationStatus.CONFIRMED:
                relation.confirmation_status = ConfirmationStatus.CONFIRMED.value

        if evidence_created:
            session.add(
                RelationEvidenceModel(
                    relation_id=relation.id,
                    source_record_version_id=binding.source_record_version_id,
                    provider=REPOSITORY_RECONCILIATION_PROVIDER,
                    source_key=(
                        f"{binding.source_alias}:{binding.source_record_id}:repository"
                    ),
                    evidence=binding.evidence_payload,
                    evidence_hash=binding.evidence_hash,
                    confidence=1.0,
                )
            )
        _record_relation_mutation(
            session,
            relation=relation,
            binding=binding,
            actor=actor,
            created=created,
            evidence_created=evidence_created,
        )
    return report.as_dict()
