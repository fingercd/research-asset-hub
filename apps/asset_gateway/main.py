from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Response
from starlette.requests import Request

app = FastAPI(
    title="Research Asset Hub Asset Gateway",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


@app.middleware("http")
async def add_isolation_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "research-asset-hub-asset-gateway", "status": "healthy"}


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "research-asset-hub-asset-gateway",
        "status": "ready",
        "detail": "No source roots are exposed by the task-1 placeholder.",
    }


@app.get("/assets/{asset_path:path}")
async def assets(asset_path: str) -> None:
    raise HTTPException(
        status_code=503,
        detail=(
            "Asset serving is disabled until a validated read-only source registry is available."
        ),
    )


def run() -> None:
    import uvicorn

    host = os.getenv("HUB_ASSET_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("HUB_ASSET_GATEWAY_PORT", "8766"))
    uvicorn.run("apps.asset_gateway.main:app", host=host, port=port)


if __name__ == "__main__":
    run()
