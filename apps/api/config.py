from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    qdrant_url: str = "http://127.0.0.1:6333"
    search_vector_version: str = "20260726-domain-expansion"
    embedding_model: str = "BAAI/bge-m3"
    embedding_batch_size: int = Field(default=8, ge=1, le=64)
    embedding_max_sequence_length: int = Field(default=512, ge=64, le=8192)
    embedding_half_precision: bool = True
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_batch_size: int = Field(default=4, ge=1, le=32)
    reranker_max_sequence_length: int = Field(default=512, ge=64, le=8192)
    search_model_idle_seconds: float = Field(default=900.0, ge=0)

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
    web_api_proxy_timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    gateway_secret: str = ""
    gateway_session_seconds: int = Field(default=900, ge=30, le=86400)
    sources_config_path: Path = PROJECT_ROOT / "config" / "sources.yaml"
    taxonomy_config_path: Path = PROJECT_ROOT / "config" / "taxonomy.yaml"
    web_dist_path: Path = PROJECT_ROOT / "apps" / "web" / "dist"
    obsidian_vault_path: Path = Path.home() / "Documents" / "Obsidian Vault"
    obsidian_hub_directory: str = "00_Home/Research Asset Hub"
    zotero_base_url: str = "http://127.0.0.1:23119"
    zotero_export_path: Path = PROJECT_ROOT / "outputs" / "zotero"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_credential_account: str = "deepseek-api-key"
    model_pricing_path: Path = PROJECT_ROOT / "config" / "model-pricing.json"
    rag_task_budget_cny: float = Field(default=5.0, gt=0)
    rag_daily_budget_cny: float = Field(default=10.0, gt=0)
    rag_monthly_budget_cny: float = Field(default=50.0, gt=0)
    rag_max_output_tokens: int = Field(default=2_000, ge=256, le=16_384)


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()
