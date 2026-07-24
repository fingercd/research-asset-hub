from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.config import get_settings
from apps.api.health import HealthResponse, collect_health

app = FastAPI(
    title="Research Asset Hub API",
    version="0.1.0",
    description="Local-first API for research assets.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Report all infrastructure states without failing when a dependency is offline."""

    return await collect_health(get_settings())


@app.get("/api", tags=["system"])
async def api_root() -> dict[str, str]:
    return {
        "service": "research-asset-hub-api",
        "status": "ready",
        "docs": "/docs",
        "health": "/api/health",
    }


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
