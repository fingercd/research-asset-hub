"""Purpose-built, conditional and idempotent correction of identifier identity."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from apps.api import routes_v1 as _v1
from apps.api.events import record_mutation, replay_idempotent
from apps.api.problem import ApiProblem, conflict, not_found
from packages.domain.enums import AuditAction
from packages.domain.identifier_policy import (
    IDENTITY_FIELD_PATH,
    identity_resolution,
    source_identity_scope,
)
from packages.domain.models import ExternalIdentifierModel, FieldResolution


class IdentityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rejected: StrictBool
    expected_asset_id: UUID
    expected_scheme: str
    expected_scope: str = ""
    expected_canonical: str
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("An audit reason is required.")
        return value


def _serialize(
    identifier: ExternalIdentifierModel, control: FieldResolution | None
) -> dict[str, Any]:
    value = cast(dict[str, Any], control.override_value) if control else {}
    return {
        "external_identifier_id": str(identifier.id),
        "asset_id": str(identifier.asset_id),
        "scheme": identifier.scheme,
        "scope": identifier.scope,
        "value": identifier.value,
        "canonical": identifier.canonical,
        "is_identity": identifier.is_identity,
        "rejected": value.get("rejected", False),
        "original_is_identity": value.get("original_is_identity", identifier.is_identity),
        "source_records": value.get("source_records", []),
        "reason": control.reason if control else None,
        "resolution_id": str(control.id) if control else None,
        "row_version": control.row_version if control else 0,
    }


@_v1.router.get("/external-identifiers/{identifier_id}/identity")
def get_identity(identifier_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        identifier = session.get(ExternalIdentifierModel, identifier_id)
        if identifier is None:
            raise not_found("External identifier", identifier_id)
        result = _serialize(identifier, identity_resolution(session, identifier.id))
        if result["resolution_id"] is None:
            result["source_records"] = source_identity_scope(session, identifier.asset_id)
        response.headers["ETag"] = _v1._etag(result["row_version"])
        return result


@_v1.router.patch("/external-identifiers/{identifier_id}/identity")
def update_identity(
    identifier_id: UUID,
    payload: IdentityPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key or not idempotency_key.strip():
        raise ApiProblem(428, "缺少幂等键", "Idempotency-Key is required.")
    if if_match is None or "*" in {part.strip() for part in if_match.split(",")}:
        raise ApiProblem(428, "缺少版本条件", "The current identity ETag is required.")
    request_payload = {
        "external_identifier_id": str(identifier_id),
        **payload.model_dump(mode="json"),
    }
    with _v1._database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="identifier-identity",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["ETag"] = _v1._etag(replay["row_version"])
            return replay
        identifier = session.get(ExternalIdentifierModel, identifier_id, with_for_update=True)
        if identifier is None:
            raise not_found("External identifier", identifier_id)
        if (identifier.asset_id, identifier.scheme, identifier.scope, identifier.canonical) != (
            payload.expected_asset_id,
            payload.expected_scheme,
            payload.expected_scope,
            payload.expected_canonical,
        ):
            raise conflict("The expected asset and identifier declaration do not match.")
        control = identity_resolution(session, identifier.id)
        before = _serialize(identifier, control)
        _v1._check_if_match(if_match, before["row_version"], required=True)
        original_identity = before["original_is_identity"]
        if not payload.rejected and original_identity:
            occupied = session.scalar(
                select(ExternalIdentifierModel.id).where(
                    ExternalIdentifierModel.id != identifier.id,
                    ExternalIdentifierModel.scheme == identifier.scheme,
                    ExternalIdentifierModel.scope == identifier.scope,
                    ExternalIdentifierModel.canonical == identifier.canonical,
                    ExternalIdentifierModel.is_identity.is_(True),
                )
            )
            if occupied is not None:
                raise conflict(
                    "Another asset already owns this identity; restoration needs review."
                )
        source_records = (
            before["source_records"]
            if control
            else source_identity_scope(session, identifier.asset_id)
        )
        if payload.rejected and not source_records:
            raise conflict("No frozen source record is associated with this asset.")
        if control is None:
            control = FieldResolution(
                entity_type="external_identifier",
                entity_id=identifier.id,
                field_path=IDENTITY_FIELD_PATH,
                resolution_kind="override",
            )
            session.add(control)
        control.override_value = {
            "asset_id": str(identifier.asset_id),
            "scheme": identifier.scheme,
            "scope": identifier.scope,
            "canonical": identifier.canonical,
            "rejected": payload.rejected,
            "original_is_identity": original_identity,
            "source_records": source_records,
        }
        control.reason = payload.reason
        control.resolved_by = "local"
        if before["row_version"]:
            control.row_version += 1
        identifier.is_identity = bool(original_identity and not payload.rejected)
        try:
            session.flush()
        except IntegrityError as error:
            raise conflict(
                "The identity was claimed by a concurrent decision; review and retry."
            ) from error
        except StaleDataError as error:
            raise ApiProblem(
                412, "版本已改变", "The identity decision changed; read its current ETag."
            ) from error
        result = _serialize(identifier, control)
        # Asset aggregate routes the existing worker to refresh all its search aliases.
        record_mutation(
            session,
            entity_type="asset",
            entity_id=identifier.asset_id,
            action=AuditAction.UPDATE if payload.rejected else AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_v1._request_id(request),
            event_type="asset.identifier-identity-updated",
            idempotency_scope="identifier-identity",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _v1._etag(result["row_version"])
        return result
