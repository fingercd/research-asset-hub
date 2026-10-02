"""Bounded HTTP reads; no database, source filesystem, or process access."""

from __future__ import annotations

import ipaddress
import json
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.server.fastmcp.exceptions import ToolError

DEFAULT_API_URL = "http://127.0.0.1:8877/api/v1"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def local_api_url(value: str) -> str:
    """Accept a loopback address only, without credentials or alternate API paths."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if host == "localhost":
            host = "127.0.0.1"
        if (
            parts.scheme not in {"http", "https"}
            or host is None
            or not ipaddress.ip_address(host).is_loopback
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path.rstrip("/") != "/api/v1"
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
    except ValueError as error:
        raise ValueError(
            "HUB_MCP_API_URL must be a loopback HTTP(S) URL ending in /api/v1."
        ) from error
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, "/api/v1/", "", ""))


class HubClient:
    """HTTP GET-only client with redirect, proxy, timeout and response-size boundaries."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = local_api_url(
            base_url if base_url is not None else os.getenv("HUB_MCP_API_URL", DEFAULT_API_URL)
        )
        self.transport = transport

    async def _read(
        self, path: str, params: dict[str, Any] | None, *, max_bytes: int
    ) -> tuple[bytes, str]:
        # Paths come exclusively from fixed tool routes, never from API-returned URLs.
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(20.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            try:
                async with client.stream("GET", path, params=params) as response:
                    status = response.status_code
                    if status != 200:
                        if status == 404:
                            message = (
                                "Requested asset, evidence, figure or code is unavailable (404)."
                            )
                        elif status in {409, 422}:
                            message = "Hub rejected this read; check the ID, source and range."
                        elif 300 <= status < 400:
                            message = "Hub returned a redirect; redirects are disabled."
                        else:
                            message = "Hub HTTP read failed."
                        raise ToolError(f"{message} HTTP {status}.")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise ToolError(
                                "Hub response exceeds the allowed size; narrow the request."
                            )
                    return bytes(payload), response.headers.get("content-type", "").split(";")[0]
            except httpx.TimeoutException as error:
                raise ToolError(
                    "Hub read timed out (connect 3s/read 20s); check library_status."
                ) from error
            except httpx.HTTPError as error:
                raise ToolError(
                    "Cannot reach the local Hub API. Start Hub yourself and check HUB_MCP_API_URL."
                ) from error

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload, mime = await self._read(path, params, max_bytes=MAX_JSON_BYTES)
        if mime != "application/json" and not mime.endswith("+json"):
            raise ToolError("Hub returned non-JSON content for a metadata read.")
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError) as error:
            raise ToolError("Hub returned invalid JSON.") from error
        if not isinstance(result, dict):
            raise ToolError("Hub returned an unexpected metadata shape.")
        return result

    async def get_image(self, path: str, *, preview: bool = True) -> tuple[bytes, str]:
        payload, mime = await self._read(
            path, {"preview": str(preview).lower()}, max_bytes=MAX_IMAGE_BYTES
        )
        if not (
            (mime == "image/png" and payload.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime == "image/jpeg" and payload.startswith(b"\xff\xd8\xff"))
        ):
            raise ToolError("Figure preview must be a PNG or JPEG with a matching file signature.")
        return payload, mime
