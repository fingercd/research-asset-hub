from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx
import psycopg as psycopg
from pydantic import BaseModel

from apps.api.config import ApiSettings

ComponentState = Literal["healthy", "unavailable", "stale"]
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_NAMED_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|token)(\s*=\s*)([^\s,;]+)")


class ComponentHealth(BaseModel):
    status: ComponentState
    detail: str
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    service: Literal["research-asset-hub-api"] = "research-asset-hub-api"
    status: Literal["healthy", "degraded"]
    checked_at: datetime
    components: dict[str, ComponentHealth]


def _safe_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    message = _URL_CREDENTIALS.sub(r"\1[redacted]@", message)
    message = _NAMED_SECRET.sub(r"\1\2[redacted]", message)
    return f"{type(error).__name__}: {message[:180]}"


def _probe_database_sync(database_url: str, timeout_seconds: float) -> None:
    """Execute the blocking psycopg probe outside the event-loop thread."""

    connect_timeout = max(1, math.ceil(timeout_seconds))
    with psycopg.connect(database_url, connect_timeout=connect_timeout) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
    if row is None or row[0] != 1:
        raise RuntimeError("PostgreSQL health query returned an unexpected result.")


async def probe_database(settings: ApiSettings) -> ComponentHealth:
    started = perf_counter()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _probe_database_sync,
                settings.database_url,
                settings.health_timeout_seconds,
            ),
            timeout=settings.health_timeout_seconds,
        )
        return ComponentHealth(
            status="healthy",
            detail="PostgreSQL accepted a query.",
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )
    except Exception as error:
        return ComponentHealth(
            status="unavailable",
            detail=_safe_error(error),
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )


async def probe_http(
    *,
    name: str,
    url: str,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
) -> ComponentHealth:
    started = perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        return ComponentHealth(
            status="healthy",
            detail=f"{name} health endpoint returned HTTP {response.status_code}.",
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )
    except Exception as error:
        return ComponentHealth(
            status="unavailable",
            detail=_safe_error(error),
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )


def probe_worker(path: Path, stale_after_seconds: float) -> ComponentHealth:
    started = perf_counter()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age_seconds = max(0.0, (datetime.now(UTC) - timestamp).total_seconds())
        state: ComponentState = "healthy" if age_seconds <= stale_after_seconds else "stale"
        return ComponentHealth(
            status=state,
            detail=(
                f"Worker heartbeat age is {age_seconds:.1f}s; "
                f"worker state is {payload.get('status', 'unknown')}."
            ),
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )
    except FileNotFoundError:
        return ComponentHealth(
            status="unavailable",
            detail=f"No worker heartbeat exists at {path}.",
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return ComponentHealth(
            status="unavailable",
            detail=_safe_error(error),
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )


async def collect_health(settings: ApiSettings) -> HealthResponse:
    database, full_text = await asyncio.gather(
        probe_database(settings),
        probe_http(
            name="Meilisearch",
            url=f"{settings.meilisearch_url.rstrip('/')}/health",
            timeout_seconds=settings.health_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.meilisearch_api_key}"},
        ),
    )
    worker = await asyncio.to_thread(
        probe_worker,
        settings.worker_heartbeat_path,
        settings.worker_stale_after_seconds,
    )
    components = {
        "database": database,
        "full_text_index": full_text,
        "worker": worker,
    }
    overall: Literal["healthy", "degraded"] = (
        "healthy"
        if all(component.status == "healthy" for component in components.values())
        else "degraded"
    )
    return HealthResponse(
        status=overall,
        checked_at=datetime.now(UTC),
        components=components,
    )
