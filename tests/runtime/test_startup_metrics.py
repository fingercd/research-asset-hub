import asyncio

from apps.startup_metrics import StartupMetrics


def test_measurement_waits_for_the_complete_desktop_get_response(capsys) -> None:
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert "DESKTOP_HOME_RESPONSE_READY" not in capsys.readouterr().out
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message["type"])

    middleware = StartupMetrics(app, ["http://tauri.localhost"])
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/dashboard",
        "headers": [(b"origin", b"http://tauri.localhost")],
    }
    asyncio.run(middleware(scope, receive, send))
    assert sent == ["http.response.start", "http.response.body", "http.response.body"]
    assert capsys.readouterr().out.count("DESKTOP_HOME_RESPONSE_READY") == 1


def test_measurement_ignores_external_requests_options_and_errors(capsys) -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": scope["test_status"], "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    for method, headers, status in [
        ("GET", [], 200),
        ("OPTIONS", [(b"origin", b"http://tauri.localhost")], 200),
        ("GET", [(b"origin", b"http://tauri.localhost")], 503),
    ]:
        middleware = StartupMetrics(app, ["http://tauri.localhost"])
        scope = {
            "type": "http",
            "method": method,
            "path": "/api/v1/dashboard",
            "headers": headers,
            "test_status": status,
        }
        asyncio.run(middleware(scope, receive, send))
        assert not middleware.reported
    assert "DESKTOP_HOME_RESPONSE_READY" not in capsys.readouterr().out
