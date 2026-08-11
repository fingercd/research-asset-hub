from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ApiSettings(BaseSettings):
    """Environment-backed settings with local-only defaults."""

    model_config = SettingsConfigDict(env_prefix="HUB_", case_sensitive=False)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8767, ge=1, le=65535)
    api_reload: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8765, ge=1, le=65535)
    asset_gateway_host: str = "127.0.0.1"
    asset_gateway_port: int = Field(default=8766, ge=1, le=65535)

    database_url: str = (
        "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub"
    )
    database_required_revision: str = "0003_search_rag_integrations"
    database_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    database_require_migrations: bool = True
    meilisearch_url: str = "http://127.0.0.1:7700"
    meilisearch_api_key: str = "research-hub-local-meili-key"

    worker_heartbeat_path: Path = Path("work/state/worker-heartbeat.json")
    worker_stale_after_seconds: float = Field(default=30.0, gt=0)
    worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_lease_seconds: int = Field(default=90, ge=10, le=3600)
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0, le=300)
    worker_max_files_per_representation: int = Field(default=1000, ge=1, le=100_000)
    worker_max_chunks_per_representation: int = Field(default=20_000, ge=1, le=1_000_000)
    worker_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    worker_tree_sitter_enabled: bool = False
    health_timeout_seconds: float = Field(default=1.5, gt=0, le=30)

    gateway_origin: str = "http://127.0.0.1:8766"
    web_origin: str = "http://127.0.0.1:8765"
    gitnexus_url: str = "http://127.0.0.1:4747"
    web_api_proxy_timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    gateway_secret: str = ""
    gateway_session_seconds: int = Field(default=900, ge=30, le=86400)
    sources_config_path: Path = PROJECT_ROOT / "config" / "sources.yaml"
    code_graph_hidden_path: Path = (
        PROJECT_ROOT / "config" / "code-graph-hidden.yaml"
    )
    taxonomy_config_path: Path = PROJECT_ROOT / "config" / "taxonomy.yaml"
    domain_portal_config_path: Path = (
        PROJECT_ROOT / "config" / "domain-portals" / "v2"
    )
    domain_portal_renderer: Literal["v1", "v2"] = "v2"
    web_dist_path: Path = PROJECT_ROOT / "apps" / "web" / "dist"
    obsidian_vault_path: Path = Path.home() / "Documents" / "Obsidian Vault"
    obsidian_hub_directory: str = "00_Home/Research Asset Hub"
    zotero_base_url: str = "http://127.0.0.1:23119"
    zotero_export_path: Path = PROJECT_ROOT / "outputs" / "zotero"
@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()
