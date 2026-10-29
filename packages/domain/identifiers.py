"""Stable identifiers used throughout the hub."""

from __future__ import annotations

import secrets
import time
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator


def new_uuid7(*, timestamp_ms: int | None = None) -> UUID:
    """Create an RFC 9562 UUIDv7 without requiring a Python 3.14 runtime.

    UUIDv7 keeps a millisecond Unix timestamp in its high bits, which makes
    database indexes locality-friendly while retaining 74 random bits.  The
    function intentionally does not promise process-wide monotonic ordering;
    uniqueness and the embedded timestamp are the required invariants.
    """

    unix_ms = int(time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms)
    if not 0 <= unix_ms < (1 << 48):
        raise ValueError("timestamp_ms must fit in the 48-bit UUIDv7 timestamp")

    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (unix_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return UUID(int=value)


def _require_uuid7(value: UUID) -> UUID:
    if value.version != 7:
        raise ValueError("identifier must be an RFC 9562 UUIDv7")
    return value


UUID7 = Annotated[UUID, AfterValidator(_require_uuid7)]
