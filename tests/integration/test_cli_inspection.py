from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.cli import inspection
from packages.cli.config import load_domain_slugs, load_sources
from packages.cli.inspection import (
    build_inventory_report,
    build_preflight_report,
    docker_status,
)
from packages.cli.main import main
from packages.domain.models import Base, FieldResolution

BASELINE_SOURCE_IDS = frozenset(
    {
        "agent",
        "infra",
        "world_model",
        "reinforcement_learning",
        "fine_tuning",
        "generation",
        "llm_tech_reports",
    }
)
BASELINE_MIN_RECORDS = 518


def test_all_configured_sources_are_read_only_and_present(local_research_assets: None) -> None:
    sources = load_sources()

    configured_ids = {source.source_id for source in sources}
    assert BASELINE_SOURCE_IDS <= configured_ids
    assert len(configured_ids) >= len(BASELINE_SOURCE_IDS)
    assert all(source.read_only for source in sources)
    assert all(source.root.is_dir() for source in sources)
    assert all(source.manifest.is_file() for source in sources)
    assert {source.domain for source in sources}.issubset(load_domain_slugs())


def test_preflight_accounts_for_all_source_records(local_research_assets: None) -> None:
    sources = load_sources()
    report = build_preflight_report(sources)

    assert report["status"] == "ready_with_warnings"
    assert report["blocking_errors"] == []
    configured = {source.source_id: source for source in sources}
    observed = {source["id"]: source for source in report["sources"]}
    assert observed.keys() == configured.keys()
    assert all(
        observed[source_id]["record_count"] == source.expected_records
        for source_id, source in configured.items()
        if source.expected_records is not None
    )
    assert sum(item["record_count"] or 0 for item in observed.values()) >= BASELINE_MIN_RECORDS


def test_preflight_detects_the_supported_wsl_docker_backend() -> None:
    status = docker_status()

    if not status["available"]:
        pytest.skip("requires a native Docker CLI or working Ubuntu WSL Docker backend")
    assert status["available"] is True
    assert status["backend"] in {"native", "wsl"}


def test_docker_status_preserves_native_backend_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        inspection,
        "command_status",
        lambda name: {"available": True, "path": "docker", "version": "Docker fixture"},
    )
    assert docker_status() == {
        "available": True,
        "path": "docker",
        "version": "Docker fixture",
        "backend": "native",
    }


@pytest.mark.parametrize("available", [True, False])
def test_docker_status_checks_wsl_backend_without_requiring_host_docker(
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
) -> None:
    monkeypatch.setattr(inspection, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        inspection,
        "command_status",
        lambda name: {"available": False, "path": None, "version": None},
    )
    monkeypatch.setattr(inspection.shutil, "which", lambda name: "wsl.exe")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:5] == ["wsl.exe", "-d", "Ubuntu", "-u", "root"]
        assert "docker version" in argv[-1]
        return subprocess.CompletedProcess(
            argv,
            0 if available else 1,
            "29.2.1\n2.42.0\n" if available else "",
            "" if available else "WSL_E_DISTRO_NOT_FOUND",
        )

    monkeypatch.setattr(inspection.subprocess, "run", fake_run)
    status = docker_status()
    assert status["available"] is available
    if available:
        assert status["backend"] == "wsl"
        assert status["version"] == "29.2.1"
        assert status["compose_version"] == "2.42.0"
    else:
        assert status["backend"] == "unavailable"
        assert status["wsl_error"] == "WSL_E_DISTRO_NOT_FOUND"


def test_inventory_accounts_for_configured_source_records(local_research_assets: None) -> None:
    report = build_inventory_report(load_sources())

    assert report["status"] == "passed"
    assert report["totals"]["source_count"] >= len(BASELINE_SOURCE_IDS)
    assert report["totals"]["source_record_count"] >= BASELINE_MIN_RECORDS
    assert report["totals"]["declared_pdf_files"] >= 427
    assert report["totals"]["declared_html_files"] >= 500


def test_system_portal_source_does_not_count_all_workspace_files_as_its_assets(
    local_research_assets: None,
) -> None:
    report = build_inventory_report(load_sources())
    portal = next(
        source for source in report["sources"] if source["id"] == "domain-portals-20260726"
    )

    assert portal["formal_html"]["matched_files"] == 10
    assert portal["visible_assets"] == {
        "pdf_files": 0,
        "html_files_in_declared_html_root": 0,
    }


def _isolated_cli_configuration(tmp_path: Path) -> Path:
    root = tmp_path / "isolated-source"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "fixture-paper",
                        "title": "Isolated CLI Checkpoint",
                        "authors": ["Fixture Author"],
                        "year": 2026,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "sources.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "cli-checkpoint-fixture",
                        "label": "CLI fixture",
                        "domain": "agent",
                        "adapter": "generic_manifest",
                        "root": str(root),
                        "manifest": "manifest.json",
                        "expected_records": 1,
                        "read_only": True,
                        "options": {"record_selector": "items"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config


def _isolated_policy_database(tmp_path: Path) -> str:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'cli-fixture.sqlite'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return database_url


def test_formal_ingest_uses_the_configured_database_after_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, int]] = []
    reports: dict[str, dict[str, Any]] = {}
    config = _isolated_cli_configuration(tmp_path)
    database_url = _isolated_policy_database(tmp_path)

    def fake_formal_import(session: Session, sources: Any) -> dict[str, Any]:
        checkpoint = reports["ingest-dry-run.json"]
        assert checkpoint["status"] == "passed"
        assert checkpoint["retirement_state_checked"] is True
        assert checkpoint["identity_decisions_checked"] is True
        assert checkpoint["totals"]["normalized_records"] == 1
        calls.append((session.get_bind().dialect.name, len(sources)))
        return {
            "schema_version": 2,
            "status": "passed",
            "source_roots_modified": False,
        }

    def capture_report(report: dict[str, Any], name: str) -> Path:
        reports[name] = deepcopy(report)
        return tmp_path / name

    monkeypatch.setenv("DATABASE_URL", database_url)
    unused_database = tmp_path / "must-not-use.sqlite"
    monkeypatch.setenv("HUB_DATABASE_URL", f"sqlite+pysqlite:///{unused_database}")
    monkeypatch.setattr(
        "packages.ingestion.formal.run_formal_import",
        fake_formal_import,
    )
    monkeypatch.setattr(
        "packages.cli.main.write_report",
        capture_report,
    )

    assert main(["--config", str(config), "ingest", "--all"]) == 0
    assert calls == [("sqlite", 1)]
    assert unused_database.exists() is False
    assert reports["ingest-formal.json"]["schema_version"] == 2
    assert reports["ingest-formal.json"]["validated_dry_run"]["status"] == "passed"
    assert reports["ingest-formal.json"]["validated_dry_run"]["normalized_records"] == 1


def test_formal_ingest_fails_closed_without_identity_policy_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _isolated_cli_configuration(tmp_path)
    database_url = _isolated_policy_database(tmp_path)
    engine = create_engine(database_url)
    FieldResolution.__table__.drop(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", database_url)

    def forbidden_formal_import(*args: Any) -> dict[str, Any]:
        pytest.fail("formal importer must not run without audited identity decisions")

    monkeypatch.setattr("packages.ingestion.formal.run_formal_import", forbidden_formal_import)
    monkeypatch.setattr("packages.cli.main.write_report", lambda report, name: tmp_path / name)
    assert main(["--config", str(config), "ingest", "--all", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["error_type"] == "OperationalError"
    assert "field_resolutions" in report["error"]


def test_formal_ingest_rejects_failed_dry_run_before_mock_importer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _isolated_cli_configuration(tmp_path)
    manifest = tmp_path / "isolated-source" / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["items"].append(document["items"][0])
    manifest.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", _isolated_policy_database(tmp_path))

    def forbidden_formal_import(*args: Any) -> dict[str, Any]:
        pytest.fail("formal importer must not run after a failed complete dry-run")

    monkeypatch.setattr("packages.ingestion.formal.run_formal_import", forbidden_formal_import)
    monkeypatch.setattr("packages.cli.main.write_report", lambda report, name: tmp_path / name)
    assert main(["--config", str(config), "ingest", "--all", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["error_type"] == "FormalDryRunGateFailed"
    assert report["dry_run_status"] == "failed"


def test_formal_ingest_rejects_partial_source_scope_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "packages.cli.main.write_report",
        lambda report, name: tmp_path / name,
    )

    assert main(["ingest", "--source", "agent", "--json"]) == 1
    output = capsys.readouterr().out
    assert "PartialFormalImportForbidden" in output
    assert "complete source set" in output
