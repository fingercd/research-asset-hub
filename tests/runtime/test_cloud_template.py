from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLOUD_ROOT = PROJECT_ROOT / "deploy" / "cloud"


def _yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_cloud_compose_has_one_public_edge_and_file_backed_secrets() -> None:
    compose = _yaml(CLOUD_ROOT / "compose.cloud.yaml")
    services = compose["services"]
    assert set(services) == {
        "caddy",
        "hub",
        "meilisearch",
        "oauth2-proxy",
        "postgres",
        "qdrant",
    }
    assert all("ports" not in service for name, service in services.items() if name != "caddy")
    assert services["hub"]["volumes"][1]["read_only"] is True
    assert services["hub"]["volumes"][1]["target"] == "/srv/research-assets"
    assert compose["networks"]["data"]["internal"] is True
    assert all(set(secret) == {"file"} for secret in compose["secrets"].values())
    assert (
        services["caddy"]["networks"]["edge"]["ipv4_address"] == "${HUB_CADDY_EDGE_IP:-172.30.0.10}"
    )
    assert any(
        command == "--trusted-proxy-ip=${HUB_CADDY_EDGE_IP:-172.30.0.10}"
        for command in services["oauth2-proxy"]["command"]
    )

    rendered = (CLOUD_ROOT / "compose.cloud.yaml").read_text(encoding="utf-8")
    assert "research_hub_local" not in rendered
    assert "sk-" not in rendered
    assert "/run/secrets/" in rendered


def test_cloud_proxy_requires_tls_and_oidc_for_both_origins() -> None:
    caddyfile = (CLOUD_ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "{$HUB_DOMAIN}" in caddyfile
    assert "{$HUB_ASSET_DOMAIN}" in caddyfile
    assert caddyfile.count("forward_auth oauth2-proxy:4180") == 2
    assert "/oauth2/auth" in caddyfile
    assert "tls_insecure_skip_verify" not in caddyfile


def test_local_compose_remains_loopback_only() -> None:
    local = _yaml(PROJECT_ROOT / "deploy" / "compose.yaml")
    published = [
        port for service in local["services"].values() for port in service.get("ports", [])
    ]
    assert published
    assert all(str(port).startswith("127.0.0.1:") for port in published)


def test_storage_contract_and_source_roots_are_read_only() -> None:
    schema = json.loads(
        (CLOUD_ROOT / "storage" / "private-storage.schema.json").read_text(encoding="utf-8")
    )
    example = _yaml(CLOUD_ROOT / "storage" / "private-storage.example.yaml")
    sources = _yaml(CLOUD_ROOT / "sources.cloud.example.yaml")

    assert schema["properties"]["spec"]["properties"]["readOnly"]["const"] is True
    assert example["spec"]["readOnly"] is True
    assert example["spec"]["driver"] == "filesystem"
    assert Path(sources["workspace_root"]).as_posix() == "/srv/research-assets"
    assert sources["sources"]
    assert all(source["read_only"] is True for source in sources["sources"])
    assert all(not Path(source["root"]).is_absolute() for source in sources["sources"])


def test_cloud_manifest_is_complete_and_marks_s3_unwired() -> None:
    manifest = json.loads((CLOUD_ROOT / "manifest.json").read_text(encoding="utf-8"))
    actual = {
        path.relative_to(CLOUD_ROOT).as_posix() for path in CLOUD_ROOT.rglob("*") if path.is_file()
    }
    assert manifest["deployed"] is False
    assert manifest["storage"]["s3-compatible"] == "not-wired"
    assert set(manifest["files"]) == actual - {"manifest.json"}


def test_cloud_secret_adapter_reads_files_and_escapes_database_password(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    values = {
        "HUB_DATABASE_URL_FILE": "p@ ss/%",
        "HUB_GATEWAY_SECRET_FILE": "g" * 48,
        "HUB_MEILISEARCH_API_KEY_FILE": "m" * 32,
    }
    for variable, value in values.items():
        secret_path = tmp_path / variable.casefold()
        secret_path.write_text(value + "\n", encoding="utf-8")
        monkeypatch.setenv(variable, str(secret_path))

    namespace = runpy.run_path(str(CLOUD_ROOT / "bin" / "run_hub.py"))
    environment = namespace["_runtime_environment"]()

    assert "p%40%20ss%2F%25" in environment["HUB_DATABASE_URL"]
    assert environment["HUB_GATEWAY_SECRET"] == "g" * 48
    assert environment["HUB_MEILISEARCH_API_KEY"] == "m" * 32
