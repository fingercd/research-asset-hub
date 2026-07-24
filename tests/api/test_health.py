from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from apps.api import health
from apps.api.config import ApiSettings


class FakeCursor:
    def __init__(self, row: tuple[int] | None = (1,)) -> None:
        self.row = row
        self.executed: list[str] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str) -> None:
        self.executed.append(query)

    def fetchone(self) -> tuple[int] | None:
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.fake_cursor


def settings(**overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {
        "database_url": "postgresql://research_hub:local-secret@127.0.0.1/research",
        "health_timeout_seconds": 0.5,
    }
    values.update(overrides)
    return ApiSettings(**values)


@pytest.mark.asyncio
async def test_database_probe_runs_sync_psycopg_in_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_thread_id = threading.get_ident()
    connection_thread_ids: list[int] = []
    cursor = FakeCursor()

    def connect(database_url: str, *, connect_timeout: int) -> FakeConnection:
        assert database_url.endswith("/research")
        assert connect_timeout == 1
        connection_thread_ids.append(threading.get_ident())
        return FakeConnection(cursor)

    monkeypatch.setattr(health.psycopg, "connect", connect)

    result = await health.probe_database(settings())

    assert result.status == "healthy"
    assert result.detail == "PostgreSQL accepted a query."
    assert cursor.executed == ["SELECT 1"]
    assert connection_thread_ids
    assert connection_thread_ids[0] != main_thread_id


@pytest.mark.asyncio
async def test_database_probe_redacts_credentials_from_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(database_url: str, *, connect_timeout: int) -> FakeConnection:
        raise RuntimeError(
            "failed for postgresql://alice:swordfish@localhost/research "
            "password=swordfish token=visible-token"
        )

    monkeypatch.setattr(health.psycopg, "connect", connect)

    result = await health.probe_database(settings())

    assert result.status == "unavailable"
    assert "swordfish" not in result.detail
    assert "visible-token" not in result.detail
    assert result.detail.count("[redacted]") == 3


@pytest.mark.asyncio
async def test_database_probe_enforces_async_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_connect(database_url: str, *, connect_timeout: int) -> FakeConnection:
        time.sleep(0.05)
        return FakeConnection(FakeCursor())

    monkeypatch.setattr(health.psycopg, "connect", slow_connect)

    result = await health.probe_database(settings(health_timeout_seconds=0.01))

    assert result.status == "unavailable"
    assert result.detail.startswith("TimeoutError:")


@pytest.mark.asyncio
async def test_database_probe_rejects_unexpected_query_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connection_factory(*args: object, **kwargs: object) -> FakeConnection:
        return FakeConnection(FakeCursor((0,)))

    monkeypatch.setattr(health.psycopg, "connect", connection_factory)

    result = await health.probe_database(settings())

    assert result.status == "unavailable"
    assert "unexpected result" in result.detail
