from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import Base


def test_configuration_and_check_do_not_require_source_cwd(tmp_path: Path) -> None:
    source = tmp_path / "sources.yaml"
    source.write_text("sources: []\n", encoding="utf-8")
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        sources_config_path=source,
        data_dir=tmp_path / "user library",
        gateway_secret="test-secret-that-must-not-enter-client-config",
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    with TestClient(create_app(settings, database=database)) as client:
        response = client.get("/api/v1/integrations/mcp-config")
        assert response.status_code == 200
        server = response.json()["config"]["mcpServers"]["research-assets"]
        assert server["args"] == ["-m", "apps.mcp_server"]
        assert server["env"]["HUB_DATA_DIR"] == str(settings.data_dir)
        assert "PYTHONPATH" in server["env"]
        assert settings.gateway_secret not in response.text
        result = client.post("/api/v1/integrations/mcp-check")
        assert result.status_code == 200
        assert result.json()["tool_count"] == 7
        assert result.json()["sample"] is None
        assert result.json()["verification"] == "bridge_construction_and_database_read"
    engine.dispose()
