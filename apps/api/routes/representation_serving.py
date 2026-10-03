"""Audited representation exclusions, independent of frozen source declarations."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy.orm import Session

from apps.api import routes_v1 as _v1
from apps.api.events import record_mutation, replay_idempotent
from apps.api.problem import ApiProblem, conflict, not_found
from packages.domain.enums import AuditAction
from packages.domain.models import FieldResolution, Representation
from packages.domain.serving_scope import (
    SERVING_FIELD_PATH,
    representation_serving_status,
    serving_resolution,
)


class ServingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    excluded: StrictBool
    expected_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    successor_representation_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("An audit reason is required.")
        return value


def _representation(
    session: Session, representation_id: UUID, *, lock: bool = False
) -> Representation:
    rep = session.get(Representation, representation_id, with_for_update=lock)
    if rep is None or rep.deleted_at is not None:
        raise not_found("Representation", representation_id)
    return rep


def _serialize(
    session: Session, rep: Representation, control: FieldResolution | None
) -> dict[str, Any]:
    status = representation_serving_status(session, rep)
    value = cast(dict[str, Any], control.override_value) if control is not None else {}
    return {
        "representation_id": str(rep.id),
        "asset_id": str(rep.asset_id),
        "excluded": bool(value.get("excluded", False)),
        "active": status.active,
        "reason": control.reason if control is not None else None,
        "expected_sha256": value.get("expected_sha256"),
        "successor_representation_id": value.get("successor_representation_id"),
        "resolution_id": str(control.id) if control is not None else None,
        "retirement_id": str(status.retirement_id) if status.retirement_id else None,
        "row_version": control.row_version if control is not None else 0,
    }


@_v1.router.get("/representations/{representation_id}/serving")
def get_serving(representation_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        rep = _representation(session, representation_id)
        result = _serialize(session, rep, serving_resolution(session, rep.id))
        response.headers["ETag"] = _v1._etag(result["row_version"])
        return result


@_v1.router.patch("/representations/{representation_id}/serving")
def update_serving(
    representation_id: UUID,
    payload: ServingPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key or not idempotency_key.strip():
        raise ApiProblem(428, "缺少幂等键", "Idempotency-Key is required.")
    if if_match is None or "*" in {part.strip() for part in if_match.split(",")}:
        raise ApiProblem(428, "缺少版本条件", "The current serving ETag is required.")
    request_payload = {
        "representation_id": str(representation_id),
        **payload.model_dump(mode="json"),
    }
    with _v1._database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="representation-serving",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["ETag"] = _v1._etag(replay["row_version"])
            return replay
        rep = _representation(session, representation_id, lock=True)
        control = serving_resolution(session, rep.id)
        before = _serialize(session, rep, control)
        _v1._check_if_match(if_match, before["row_version"], required=True)
        if rep.sha256 is None or rep.sha256.lower() != payload.expected_sha256.lower():
            raise conflict("The expected SHA-256 does not match this representation.")
        if payload.successor_representation_id is not None:
            successor = _representation(session, payload.successor_representation_id)
            if (
                not payload.excluded
                or successor.id == rep.id
                or successor.asset_id != rep.asset_id
                or successor.representation_type != rep.representation_type
                or successor.availability != "available"
                or not representation_serving_status(session, successor).active
            ):
                raise conflict(
                    "A successor must be a different available, active representation "
                    "of the same type and asset."
                )
        value = {
            "excluded": payload.excluded,
            "expected_sha256": payload.expected_sha256.lower(),
            "successor_representation_id": (
                str(payload.successor_representation_id)
                if payload.successor_representation_id
                else None
            ),
        }
        if control is None:
            control = FieldResolution(
                entity_type="representation",
                entity_id=rep.id,
                field_path=SERVING_FIELD_PATH,
                resolution_kind="override",
            )
            session.add(control)
        control.override_value = value
        control.reason = payload.reason
        control.resolved_by = "local"
        # Increment even for an explicitly repeated decision with a new reason/key.
        # The representation/source declaration and its row_version remain untouched.
        if before["row_version"]:
            control.row_version += 1
        session.flush()
        result = _serialize(session, rep, control)
        record_mutation(
            session,
            entity_type="representation_serving",
            entity_id=rep.id,
            action=AuditAction.UPDATE if payload.excluded else AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_v1._request_id(request),
            event_type="representation.serving-updated",
            idempotency_scope="representation-serving",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _v1._etag(control.row_version)
        return result
