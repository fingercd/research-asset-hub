from __future__ import annotations

import base64
import json

from apps.api.problem import ApiProblem


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        offset = int(value["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ApiProblem(
            400,
            "游标无效",
            "The pagination cursor is malformed or expired.",
            problem_type="urn:research-asset-hub:problem:cursor",
        ) from error
