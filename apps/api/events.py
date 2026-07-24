from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.api.problem import conflict
from packages.domain.enums import AuditAction, OutboxStatus
from packages.domain.models import AuditEvent, OutboxEvent


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def idempotency_dedupe_key(scope: str, key: str) -> str:
    return f"api:{scope}:{hashlib.sha256(key.encode()).hexdigest()}"


def replay_idempotent(
    session: Session,
    *,
    scope: str,
    key: str | None,
    request_payload: Any,
) -> dict[str, Any] | None:
    if not key:
        return None
    dedupe_key = idempotency_dedupe_key(scope, key)
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        lock_value = int.from_bytes(
            hashlib.sha256(dedupe_key.encode()).digest()[:8],
            "big",
            signed=True,
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_value)"),
            {"lock_value": lock_value},
        )
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key))
    if event is None:
        return None
    idempotency = event.payload.get("idempotency", {})
    if idempotency.get("request_hash") != stable_hash(request_payload):
        raise conflict("The Idempotency-Key was already used with a different request.")
    response = idempotency.get("response")
    return dict(response) if isinstance(response, dict) else None


def record_mutation(
    session: Session,
    *,
    entity_type: str,
    entity_id: UUID,
    action: AuditAction,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    request_id: str | None,
    event_type: str,
    idempotency_scope: str | None = None,
    idempotency_key: str | None = None,
    request_payload: Any = None,
    response_payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action.value,
            request_id=request_id,
            before=before,
            after=after,
            details={},
        )
    )
    if idempotency_scope and idempotency_key:
        dedupe_key = idempotency_dedupe_key(idempotency_scope, idempotency_key)
    else:
        dedupe_key = f"event:{event_type}:{entity_id}:{uuid4().hex}"
    payload: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "before": before,
        "after": after,
    }
    if idempotency_scope and idempotency_key:
        payload["idempotency"] = {
            "request_hash": stable_hash(request_payload),
            "response": response_payload,
        }
    session.add(
        OutboxEvent(
            aggregate_type=entity_type,
            aggregate_id=entity_id,
            event_type=event_type,
            payload=payload,
            status=OutboxStatus.PENDING.value,
            dedupe_key=dedupe_key,
        )
    )
