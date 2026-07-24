"""Persist audited HTML sandbox policies without touching source files."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from packages.domain.enums import AuditAction, OutboxStatus
from packages.domain.models import AuditEvent, OutboxEvent, Representation

from .html_audit import HtmlAuditReport

_POLICIES = frozenset({"static", "trusted_local", "network_required"})


@dataclass(frozen=True, slots=True)
class HtmlPolicyInference:
    representation_id: UUID
    policy: str
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.policy not in _POLICIES:
            raise ValueError(f"Unsupported HTML sandbox policy: {self.policy}")


def inferences_from_audit(report: HtmlAuditReport) -> list[HtmlPolicyInference]:
    return [
        HtmlPolicyInference(
            representation_id=page.registration.representation_id,
            policy=page.inferred_policy,
            content_sha256=page.content_sha256,
        )
        for page in report.pages
        if page.inferred_policy in _POLICIES
        and page.static_status in {"audited", "truncated"}
    ]


def apply_html_policy_inferences(
    session: Session,
    inferences: Iterable[HtmlPolicyInference],
    *,
    actor: str = "system:html-audit",
) -> dict[str, object]:
    """Apply changed policies with per-representation audit and outbox records."""

    counters: Counter[str] = Counter()
    changed_ids: list[str] = []
    for inference in inferences:
        representation = session.get(
            Representation,
            inference.representation_id,
            with_for_update=True,
        )
        if representation is None or representation.deleted_at is not None:
            counters["missing"] += 1
            continue
        attributes = dict(representation.attributes or {})
        current_policy = str(attributes.get("sandbox_policy") or "static")
        current_network = representation.network_required
        target_network = inference.policy == "network_required"
        if current_policy == inference.policy and current_network == target_network:
            counters["unchanged"] += 1
            continue

        before = {
            "sandbox_policy": current_policy,
            "network_required": current_network,
        }
        attributes["sandbox_policy"] = inference.policy
        attributes["sandbox_policy_source"] = "static-html-audit/v1"
        if inference.content_sha256:
            attributes["sandbox_policy_content_sha256"] = inference.content_sha256
        representation.attributes = attributes
        representation.network_required = target_network
        after = {
            "sandbox_policy": inference.policy,
            "network_required": target_network,
            "content_sha256": inference.content_sha256,
        }
        session.add(
            AuditEvent(
                entity_type="representation",
                entity_id=representation.id,
                action=AuditAction.UPDATE.value,
                actor=actor,
                before=before,
                after=after,
                details={"reason": "html_sandbox_policy_inference"},
            )
        )
        session.add(
            OutboxEvent(
                aggregate_type="representation",
                aggregate_id=representation.id,
                event_type="representation.html-policy-updated",
                payload={"before": before, "after": after},
                status=OutboxStatus.PENDING.value,
                dedupe_key=(
                    f"html-policy:{representation.id}:{inference.policy}:"
                    f"{inference.content_sha256 or 'unknown'}"
                ),
            )
        )
        counters["updated"] += 1
        counters[f"policy_{inference.policy}"] += 1
        changed_ids.append(str(representation.id))

    return {
        "updated": counters["updated"],
        "unchanged": counters["unchanged"],
        "missing": counters["missing"],
        "policies": {
            policy: counters[f"policy_{policy}"]
            for policy in sorted(_POLICIES)
        },
        "representation_ids": changed_ids,
    }


__all__ = [
    "HtmlPolicyInference",
    "apply_html_policy_inferences",
    "inferences_from_audit",
]
