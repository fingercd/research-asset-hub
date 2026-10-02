"""Opt-in measurement of the real desktop homepage response, without another query."""

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class StartupMetrics:
    def __init__(self, app: ASGIApp, origins: Iterable[str]) -> None:
        self.app = app
        self.origins = {origin.encode("utf-8") for origin in origins}
        self.reported = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.reported
            or scope["type"] != "http"
            or scope["method"] != "GET"
            or scope["path"] != "/api/v1/dashboard"
            or not any(
                name == b"origin" and value in self.origins
                for name, value in scope.get("headers", [])
            )
        ):
            await self.app(scope, receive, send)
            return
        status = 0

        async def measured_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and status == 200
                and not self.reported
            ):
                self.reported = True
                print("DESKTOP_HOME_RESPONSE_READY", flush=True)

        await self.app(scope, receive, measured_send)
