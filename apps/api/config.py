from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    """Environment-backed settings with local-only defaults."""

    model_config = SettingsConfigDict(env_prefix="HUB_", case_sensitive=False)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8767, ge=1, le=65535)
    api_reload: bool = False

    database_url: str = (
        "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub"
    )
    meilisearch_url: str = "http://127.0.0.1:7700"
    meilisearch_api_key: str = "research-hub-local-meili-key"
    qdrant_url: str = "http://127.0.0.1:6333"

    worker_heartbeat_path: Path = Path("work/state/worker-heartbeat.json")
    worker_stale_after_seconds: float = Field(default=30.0, gt=0)
    health_timeout_seconds: float = Field(default=1.5, gt=0, le=30)


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()
