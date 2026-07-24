from __future__ import annotations

import json

import httpx
import pytest

from packages.indexing.meili import HttpxJSONTransport


def test_transport_batches_then_verifies_queued_tasks() -> None:
    next_uid = 1
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal next_uid, polls
        if request.method == "POST":
            uid = next_uid
            next_uid += 1
            return httpx.Response(202, json={"taskUid": uid})
        assert request.url.path == "/tasks"
        polls += 1
        statuses = ("processing", "succeeded") if polls == 1 else ("succeeded", "succeeded")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"uid": uid, "status": status}
                    for uid, status in zip((1, 2), statuses, strict=True)
                ]
            },
        )

    transport = HttpxJSONTransport(
        "http://meili.test",
        wait_for_tasks=False,
    )
    transport.client.close()
    transport.client = httpx.Client(
        base_url="http://meili.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport.request(
            "POST",
            "/indexes/code/documents",
            payload={"documents": [{"document_id": "one"}]},
        )
        transport.request(
            "POST",
            "/indexes/code/documents",
            payload={"documents": [{"document_id": "two"}]},
        )
        transport.flush_pending_tasks(timeout_seconds=1, poll_seconds=0)
    finally:
        transport.close()

    assert polls == 2


def test_transport_reports_a_failed_queued_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"taskUid": 7})
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "results": [
                        {
                            "uid": 7,
                            "status": "failed",
                            "error": {"code": "invalid_document"},
                        }
                    ]
                }
            ).encode(),
        )

    transport = HttpxJSONTransport(
        "http://meili.test",
        wait_for_tasks=False,
    )
    transport.client.close()
    transport.client = httpx.Client(
        base_url="http://meili.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport.request(
            "POST",
            "/indexes/code/documents",
            payload={"documents": [{"document_id": "bad"}]},
        )
        with pytest.raises(RuntimeError, match="invalid_document"):
            transport.flush_pending_tasks(timeout_seconds=1, poll_seconds=0)
    finally:
        transport.close()
