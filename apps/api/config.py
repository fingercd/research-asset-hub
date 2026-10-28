from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from packages.managed_sources.service import default_data_directory

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ApiSettings(BaseSettings):
    """Environment-backed settings with local-only defaults."""

    model_config = SettingsConfigDict(env_prefix="HUB_", case_sensitive=False)

    data_dir: Path = Field(default_factory=default_data_directory)
    intake_max_pdf_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    intake_reference_roots: list[Path] = Field(default_factory=list)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8767, ge=1, le=65535)
    api_reload: bool = False
    asset_gateway_host: str = "127.0.0.1"
    asset_gateway_port: int = Field(default=8766, ge=1, le=65535)

    database_url: str = (
        "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub"
    )
    # 最低要求的数据库迁移修订（>= 语义：数据库修订是它本身或其后代即可）。
    # tests/domain/test_migrations.py 断言该默认值与最新迁移 head 一致。
    database_required_revision: str = "0007_catalog_visibility_index"
    database_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    database_require_migrations: bool = True
    meilisearch_url: str = "http://127.0.0.1:7700"
    meilisearch_api_key: str = "research-hub-local-meili-key"
    # 代码全文索引默认不参与查询；本地代码资产和代码按钮仍正常可用。
    search_code_index_enabled: bool = False

    worker_heartbeat_path: Path = Path("work/state/worker-heartbeat.json")
    worker_stale_after_seconds: float = Field(default=30.0, gt=0)
    worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_lease_seconds: int = Field(default=90, ge=10, le=3600)
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0, le=300)
    worker_max_files_per_representation: int = Field(default=1000, ge=1, le=100_000)
    worker_max_chunks_per_representation: int = Field(default=20_000, ge=1, le=1_000_000)
    worker_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    # 代码符号索引默认关闭；置 true 启用 tree-sitter 符号抽取（依赖已随包安装）。
    worker_tree_sitter_enabled: bool = False
    health_timeout_seconds: float = Field(default=1.5, gt=0, le=30)

    gateway_origin: str = "http://127.0.0.1:8766"
    desktop_origins: Annotated[list[str], NoDecode] = Field(
        default=["http://tauri.localhost", "tauri://localhost"],
        description=(
            "桌面壳(Tauri WebView2)的浏览器 Origin,纳入 CORS 与写保护白名单。"
            "环境变量 HUB_DESKTOP_ORIGINS 用逗号分隔覆盖。"
        ),
    )
    gateway_secret: str = ""
    gateway_session_seconds: int = Field(default=900, ge=30, le=86400)
    sources_config_path: Path = PROJECT_ROOT / "config" / "sources.yaml"
    host_workspace_root: Path = PROJECT_ROOT
    taxonomy_config_path: Path = PROJECT_ROOT / "config" / "taxonomy.yaml"
    domain_portal_config_path: Path = PROJECT_ROOT / "config" / "domain-portals" / "v2"
    domain_portal_renderer: Literal["v1", "v2"] = "v2"
    obsidian_vault_path: Path = Path.home() / "Documents" / "Obsidian Vault"
    obsidian_hub_directory: str = "00_Home/Research Asset Hub"
    zotero_base_url: str = "http://127.0.0.1:23119"
    zotero_export_path: Path = PROJECT_ROOT / "outputs" / "zotero"

    @model_validator(mode="after")
    def use_user_configuration(self) -> ApiSettings:
        config = self.data_dir / "config"
        if (config / "sources.yaml").is_file():
            for field, relative in (
                ("sources_config_path", "sources.yaml"),
                ("taxonomy_config_path", "taxonomy.yaml"),
                ("domain_portal_config_path", "domain-portals/v2"),
            ):
                if field not in self.model_fields_set:
                    setattr(self, field, config / relative)
        return self

    @field_validator("desktop_origins", mode="before")
    @classmethod
    def _split_desktop_origins(cls, value: object) -> object:
        """把逗号分隔的环境变量串拆成列表;已是列表则原样返回。"""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()
