from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class InvalidGatewayToken(ValueError):
    """Raised when a gateway session token is invalid or expired."""


@dataclass(frozen=True, slots=True)
class GatewayGrant:
    representation_id: UUID
    expires_at: int
    nonce: str


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, UnicodeEncodeError) as error:
        raise InvalidGatewayToken("Malformed gateway token.") from error


def create_gateway_token(
    secret: str,
    representation_id: UUID,
    *,
    ttl_seconds: int,
    nonce: str,
    now: int | None = None,
) -> str:
    if len(secret) < 32:
        raise ValueError("HUB_GATEWAY_SECRET must contain at least 32 characters.")
    issued_at = int(time.time()) if now is None else now
    payload = {
        "rid": str(representation_id),
        "exp": issued_at + ttl_seconds,
        "nonce": nonce,
    }
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _encode(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


def verify_gateway_token(
    secret: str,
    token: str,
    *,
    representation_id: UUID | None = None,
    now: int | None = None,
) -> GatewayGrant:
    if len(secret) < 32:
        raise InvalidGatewayToken("Gateway signing is not configured.")
    try:
        body, signature = token.split(".", 1)
    except ValueError as error:
        raise InvalidGatewayToken("Malformed gateway token.") from error
    expected = _encode(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        raise InvalidGatewayToken("Gateway token signature is invalid.")
    try:
        data: Any = json.loads(_decode(body))
        grant = GatewayGrant(
            representation_id=UUID(str(data["rid"])),
            expires_at=int(data["exp"]),
            nonce=str(data["nonce"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidGatewayToken("Gateway token payload is invalid.") from error
    current_time = int(time.time()) if now is None else now
    if grant.expires_at < current_time:
        raise InvalidGatewayToken("Gateway token has expired.")
    if representation_id is not None and grant.representation_id != representation_id:
        raise InvalidGatewayToken("Gateway token does not grant this representation.")
    return grant
