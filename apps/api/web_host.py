from __future__ import annotations

import mimetypes
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from apps.api.config import ApiSettings, get_settings

_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "idempotency-key",
    "if-match",
    "origin",
    "x-research-hub-csrf",
    "x-request-id",
}
_RESPONSE_HEADERS = {
    "content-type",
    "etag",
    "idempotency-replayed",
    "retry-after",
    "x-request-id",
}


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    api_origin = f"http://127.0.0.1:{app_settings.api_port}"
    client = httpx.AsyncClient(base_url=api_origin, timeout=60)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    application = FastAPI(
        title="Research Asset Hub Web Host",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @application.api_route(
        "/api/{api_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def api_proxy(api_path: str, request: Request) -> Response:
        try:
            upstream = await client.request(
                request.method,
                f"/api/{api_path}",
                params=request.query_params,
                content=await request.body(),
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.casefold() in _REQUEST_HEADERS
                },
            )
        except httpx.HTTPError as error:
            return JSONResponse(
                {
                    "type": "urn:research-asset-hub:problem:api-unavailable",
                    "title": "本地 API 不可用",
                    "status": 503,
                    "detail": f"{type(error).__name__}: could not reach the local API.",
                    "instance": request.url.path,
                },
                status_code=503,
                media_type="application/problem+json",
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                key: value
                for key, value in upstream.headers.items()
                if key.casefold() in _RESPONSE_HEADERS
            },
        )

    @application.get("/{asset_path:path}")
    async def spa(asset_path: str) -> Response:
        dist = app_settings.web_dist_path.resolve()
        requested = (dist / asset_path).resolve(strict=False)
        try:
            requested.relative_to(dist)
        except ValueError:
            return Response(status_code=404)
        if requested.is_file():
            return FileResponse(
                requested,
                media_type=mimetypes.guess_type(requested.name)[0],
            )
        index = dist / "index.html"
        if not index.is_file():
            return JSONResponse(
                {
                    "type": "urn:research-asset-hub:problem:web-dist-missing",
                    "title": "前端构建尚不存在",
                    "status": 503,
                    "detail": "Run the frontend production build before hub start.",
                },
                status_code=503,
                media_type="application/problem+json",
            )
        return FileResponse(index, media_type="text/html")

    return application


app = create_app()


def run() -> None:
    import uvicorn

    host = os.getenv("HUB_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("HUB_WEB_PORT", "8765"))
    uvicorn.run("apps.api.web_host:app", host=host, port=port)


if __name__ == "__main__":
    run()
