from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response

from apps.api.config import ApiSettings, get_settings
from apps.api.database import DatabaseManager
from apps.api.health import ComponentHealth, HealthResponse, collect_health
from apps.api.problem import install_problem_handlers
from apps.api.routes_v1 import router as v1_router
from apps.api.search_runtime import SearchRuntime


def create_app(
    settings: ApiSettings | None = None,
    *,
    database: DatabaseManager | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    database_manager = database or DatabaseManager(app_settings)
    owns_database = database is None

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            search_runtime = getattr(lifespan_app.state, "search_runtime", None)
            if isinstance(search_runtime, SearchRuntime):
                await asyncio.to_thread(search_runtime.close)
            if owns_database:
                database_manager.dispose()

    application = FastAPI(
        title="Research Asset Hub API",
        version="0.1.0",
        description="Local-first API for research assets.",
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.database = database_manager
    application.state.search_runtime = None
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "If-Match",
            "Idempotency-Key",
            "X-Request-ID",
        ],
        expose_headers=["ETag", "X-Request-ID", "Idempotency-Replayed"],
    )

    @application.middleware("http")
    async def enforce_browser_write_origin(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None:
                allowed_origins = {
                    app_settings.web_origin,
                    app_settings.web_origin.replace("127.0.0.1", "localhost"),
                    "http://testserver",
                }
                csrf_token = request.headers.get("x-research-hub-csrf")
                if origin not in allowed_origins or csrf_token != "1":
                    return JSONResponse(
                        {
                            "type": "urn:research-asset-hub:problem:csrf",
                            "title": "浏览器写请求被拒绝",
                            "status": 403,
                            "code": "csrf_rejected",
                            "detail": (
                                "Unsafe browser requests require an allowed local Origin "
                                "and X-Research-Hub-CSRF: 1."
                            ),
                            "instance": request.url.path,
                            "request_id": getattr(
                                request.state,
                                "request_id",
                                None,
                            ),
                        },
                        status_code=403,
                        media_type="application/problem+json",
                    )
        return await call_next(request)

    install_problem_handlers(application)
    application.include_router(v1_router)

    @application.get("/api/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        """Report dependencies and distinguish an unavailable DB from an unmigrated DB."""

        result = await collect_health(app_settings)
        readiness = await asyncio.to_thread(database_manager.check, force=True)
        if readiness.status == "ready":
            result.components["database"] = ComponentHealth(
                status="healthy",
                detail=readiness.detail,
            )
        else:
            result.components["database"] = ComponentHealth(
                status="unavailable",
                detail=f"{readiness.status}: {readiness.detail}",
            )
            result.status = "degraded"
        return result

    @application.get("/api", tags=["system"])
    def api_root() -> dict[str, str]:
        readiness = database_manager.check()
        return {
            "service": "research-asset-hub-api",
            "status": "ready" if readiness.status == "ready" else "degraded",
            "database": readiness.status,
            "docs": "/docs",
            "health": "/api/health",
        }

    return application


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "apps.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
    )


if __name__ == "__main__":
    run()
