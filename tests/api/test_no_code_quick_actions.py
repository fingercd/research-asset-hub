from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api import routes_v1
from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.api.main import create_app
from packages.domain.models import (
    Asset,
    BackupRun,
    Base,
    Relation,
    Representation,
    Source,
    SourceRetirement,
)
from packages.ingestion import safe_tree_policy


@pytest.fixture
def no_code_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, DatabaseManager, Path]]:
    allowlist_path = tmp_path / "check-unit-actions-allowlist.yaml"
    allowlist_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(routes_v1, "_NO_CODE_ALLOWLIST_PATH", allowlist_path)

    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
        gateway_secret="n" * 64,
        sources_config_path=tmp_path / "sources.yaml",
        taxonomy_config_path=tmp_path / "taxonomy.yaml",
    )
    settings.sources_config_path.write_text(
        (
            "schema_version: 1\n"
            "workspace_root: .\n"
            "sources:\n"
            "  - id: local-code\n"
            "    root: .\n"
            "    asset_roots:\n"
            "      repository: repos\n"
        ),
        encoding="utf-8",
    )
    settings.taxonomy_config_path.write_text(
        "schema_version: 1\ndomains: []\n",
        encoding="utf-8",
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    database = DatabaseManager(settings, engine=engine, require_migrations=False)
    app = create_app(settings, database=database)
    with TestClient(app) as client:
        yield client, database, allowlist_path
    engine.dispose()


def _write_allowlist(path: Path, policies: dict[str, list[str]]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                domain: {"no_code_titles": titles}
                for domain, titles in policies.items()
            },
            allow_unicode=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _seed_paper_repository(
    database: DatabaseManager,
    *,
    title: str,
    domains: list[str],
    remote: bool = True,
    local: bool = False,
    source_id: UUID | None = None,
    local_source_id: UUID | None = None,
    local_path_ref: str = "official-code",
) -> tuple[UUID, UUID, dict[str, UUID]]:
    with database.session() as session:
        paper = Asset(asset_type="paper", title=title, domain_slugs=domains)
        repository = Asset(
            asset_type="repository",
            title=f"{title} implementation",
            domain_slugs=domains,
        )
        session.add_all([paper, repository])
        session.flush()
        session.add(
            Relation(
                subject_asset_id=paper.id,
                object_asset_id=repository.id,
                relation_type="implemented_by",
                confirmation_status="confirmed",
            )
        )
        representation_ids: dict[str, UUID] = {}
        if remote:
            remote_representation = Representation(
                asset_id=repository.id,
                source_id=source_id,
                representation_type="repository",
                role="official",
                materialization_kind="url",
                availability="available",
                external_url="https://example.invalid/project-placeholder",
                network_required=True,
            )
            session.add(remote_representation)
            session.flush()
            representation_ids["remote"] = remote_representation.id
        if local:
            local_representation = Representation(
                asset_id=repository.id,
                source_id=local_source_id,
                representation_type="repository",
                role="official",
                materialization_kind="directory",
                availability="available",
                root_key="repository",
                path_ref=local_path_ref,
                network_required=False,
            )
            session.add(local_representation)
            session.flush()
            representation_ids["local"] = local_representation.id
        return paper.id, repository.id, representation_ids


def _registered_local_source(
    database: DatabaseManager,
    *,
    root_path: Path,
) -> UUID:
    with database.session() as session:
        source = Source(
            alias="local-code",
            label="Local code",
            adapter="generic_manifest",
            root_path=str(root_path),
        )
        session.add(source)
        session.flush()
        return source.id


def _paper_code_action(client: TestClient, paper_id: UUID) -> dict[str, Any]:
    response = client.get(
        "/api/v1/search",
        params={"group_by": "paper", "type": "paper"},
    )
    assert response.status_code == 200, response.text
    unit = next(
        item for item in response.json()["items"] if item["unit_id"] == str(paper_id)
    )
    return next(
        action for action in unit["quick_actions"] if action["kind"] == "code"
    )


@pytest.mark.parametrize(
    ("domain", "title"),
    [
        (
            "embodied-ai-compression",
            "EcoVLA: Environment-Aware Adaptive Pruning with Interleaved "
            "Inference Orchestration for Vision-Language-Action Models",
        ),
        (
            "embodied-ai-compression",
            "EfficientVLA: Training-Free Acceleration and Compression for "
            "Vision-Language-Action Models",
        ),
        (
            "video-llm-compression",
            "QueryStream: Advancing Streaming Video Understanding with "
            "Query-Aware Pruning and Proactive Response",
        ),
        (
            "navigation-compression",
            "Cross from Left to Right Brain: Adaptive Text Dreamer for "
            "Vision-and-Language Navigation",
        ),
    ],
)
def test_allowlisted_announced_url_is_reported_as_not_provided(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
    domain: str,
    title: str,
) -> None:
    client, database, allowlist_path = no_code_api
    _write_allowlist(allowlist_path, {domain: [title]})
    paper_id, _, representations = _seed_paper_repository(
        database,
        title=title,
        domains=[domain],
    )

    action = _paper_code_action(client, paper_id)

    assert action == {
        "kind": "code",
        "status": "not_provided",
        "representation_id": None,
        "owner_asset_id": str(paper_id),
        "relation_type": "implemented_by",
        "reason": routes_v1._NO_CODE_AUDIT_REASON,
        "alternatives": [],
    }
    assert str(representations["remote"]) not in str(action)


@pytest.mark.parametrize(
    "title",
    [
        "AwareVLN: Reasoning with Self-awareness for Vision-Language Navigation",
        (
            "Ground Slow, Move Fast: A Dual-System Foundation Model for "
            "Generalizable Vision-Language Navigation"
        ),
    ],
)
def test_allowlist_never_suppresses_an_available_local_repository(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
    title: str,
) -> None:
    client, database, allowlist_path = no_code_api
    _write_allowlist(allowlist_path, {"navigation-compression": [title]})
    local_path = allowlist_path.parent / "repos" / "official-code"
    local_path.mkdir(parents=True)
    local_source_id = _registered_local_source(
        database,
        root_path=allowlist_path.parent,
    )
    paper_id, repository_id, representations = _seed_paper_repository(
        database,
        title=title,
        domains=["navigation-compression"],
        local=True,
        local_source_id=local_source_id,
    )

    action = _paper_code_action(client, paper_id)

    assert action["status"] == "available"
    assert action["representation_id"] == str(representations["local"])
    assert action["owner_asset_id"] == str(repository_id)
    assert action["alternatives"] == [
        {
            "representation_id": str(representations["remote"]),
            "owner_asset_id": str(repository_id),
            "label": f"{title} implementation",
            "role": "official",
            "status": "network_required",
            "relation_type": "implemented_by",
        }
    ]


@pytest.mark.parametrize(
    "invalid_path_ref",
    ["missing-repository", "../outside-repository"],
)
def test_unservable_local_path_does_not_override_no_code_audit(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
    invalid_path_ref: str,
) -> None:
    client, database, allowlist_path = no_code_api
    title = f"Audited Paper with Invalid Local Path {invalid_path_ref}"
    _write_allowlist(allowlist_path, {"agent": [title]})
    local_source_id = _registered_local_source(
        database,
        root_path=allowlist_path.parent,
    )
    paper_id, _, representations = _seed_paper_repository(
        database,
        title=title,
        domains=["agent"],
        local=True,
        local_source_id=local_source_id,
        local_path_ref=invalid_path_ref,
    )

    action = _paper_code_action(client, paper_id)

    assert action["status"] == "not_provided"
    assert action["representation_id"] is None
    assert action["alternatives"] == []
    assert str(representations["local"]) not in str(action)
    assert str(representations["remote"]) not in str(action)


def test_reparse_local_path_does_not_override_no_code_audit(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, allowlist_path = no_code_api
    title = "Audited Paper with Reparse Local Path"
    _write_allowlist(allowlist_path, {"agent": [title]})
    repository_root = allowlist_path.parent / "repos"
    real_repository = repository_root / "real-repository"
    real_repository.mkdir(parents=True)
    linked_repository = repository_root / "linked-repository"
    try:
        linked_repository.symlink_to(real_repository, target_is_directory=True)
    except OSError:
        linked_repository.mkdir()
        original_lstat = safe_tree_policy.lstat

        def simulated_reparse_lstat(path: Path) -> Any:
            if Path(path) == linked_repository:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return original_lstat(path)

        monkeypatch.setattr(safe_tree_policy, "lstat", simulated_reparse_lstat)
    local_source_id = _registered_local_source(
        database,
        root_path=allowlist_path.parent,
    )
    paper_id, _, representations = _seed_paper_repository(
        database,
        title=title,
        domains=["agent"],
        local=True,
        local_source_id=local_source_id,
        local_path_ref="linked-repository",
    )

    action = _paper_code_action(client, paper_id)

    assert action["status"] == "not_provided"
    assert action["representation_id"] is None
    assert action["alternatives"] == []
    assert str(representations["local"]) not in str(action)
    assert str(representations["remote"]) not in str(action)


def test_non_allowlisted_url_remains_network_required(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
) -> None:
    client, database, allowlist_path = no_code_api
    _write_allowlist(allowlist_path, {"agent": ["unreviewed url paper"]})
    paper_id, repository_id, representations = _seed_paper_repository(
        database,
        title="Unreviewed URL Paper",
        domains=["agent"],
    )

    action = _paper_code_action(client, paper_id)

    assert action["status"] == "network_required"
    assert action["representation_id"] == str(representations["remote"])
    assert action["owner_asset_id"] == str(repository_id)
    assert action["reason"] == "该入口需要联网。"


def test_exact_title_can_be_allowlisted_by_any_paper_domain(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
) -> None:
    client, database, allowlist_path = no_code_api
    title = "Cross-Domain Audited Paper"
    _write_allowlist(
        allowlist_path,
        {
            "agent": ["Another Paper"],
            "video-llm-compression": [title],
        },
    )
    paper_id, _, _ = _seed_paper_repository(
        database,
        title=title,
        domains=["agent", "video-llm-compression"],
    )

    action = _paper_code_action(client, paper_id)

    assert action["status"] == "not_provided"
    assert action["representation_id"] is None


def test_allowlist_does_not_change_independent_repository_actions(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
) -> None:
    client, database, allowlist_path = no_code_api
    title = "Repository Title Also Present in Audit List"
    _write_allowlist(allowlist_path, {"agent": [title]})
    with database.session() as session:
        repository = Asset(
            asset_type="repository",
            title=title,
            domain_slugs=["agent"],
        )
        session.add(repository)
        session.flush()
        representation = Representation(
            asset_id=repository.id,
            representation_type="repository",
            role="official",
            materialization_kind="url",
            availability="available",
            external_url="https://example.invalid/independent",
            network_required=True,
        )
        session.add(representation)
        session.flush()
        repository_id = repository.id
        representation_id = representation.id

    response = client.get(
        "/api/v1/search",
        params={"q": title, "group_by": "paper", "type": "all"},
    )

    assert response.status_code == 200, response.text
    unit = next(
        item
        for item in response.json()["items"]
        if item["unit_id"] == str(repository_id)
    )
    action = next(
        item for item in unit["quick_actions"] if item["kind"] == "code"
    )
    assert unit["unit_kind"] == "repository"
    assert action["status"] == "network_required"
    assert action["representation_id"] == str(representation_id)


def test_retired_url_does_not_reappear_in_allowlisted_paper_action(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
) -> None:
    client, database, allowlist_path = no_code_api
    title = "Audited Paper with Retired Placeholder"
    _write_allowlist(allowlist_path, {"agent": [title]})
    digest = "a" * 64
    with database.session() as session:
        source = Source(
            alias="retired-placeholder-source",
            label="Retired placeholder source",
            adapter="generic_manifest",
            root_path="retired-placeholder-source",
        )
        backup = BackupRun(destination_label="no-code-retirement-test")
        session.add_all([source, backup])
        session.flush()
        source_id = source.id
        session.add(
            SourceRetirement(
                source_id=source.id,
                backup_id=backup.id,
                payload={"source_id": str(source.id)},
                payload_digest="b" * 64,
                frozen_manifest_sha256=digest,
                frozen_source_config_sha256=digest,
                record_set_digest=digest,
                representation_set_digest=digest,
                mount_set_digest=digest,
                collection_digest=digest,
                signature_algorithm="test",
                signer_key_id="test",
                signature="test",
                applied_by="pytest",
            )
        )
    paper_id, _, representations = _seed_paper_repository(
        database,
        title=title,
        domains=["agent"],
        source_id=source_id,
    )

    action = _paper_code_action(client, paper_id)

    assert action["status"] == "not_provided"
    assert action["representation_id"] is None
    assert action["alternatives"] == []
    assert str(representations["remote"]) not in str(action)


@pytest.mark.parametrize("failure", ["missing", "malformed"])
def test_allowlist_configuration_errors_fail_closed(
    no_code_api: tuple[TestClient, DatabaseManager, Path],
    failure: str,
) -> None:
    client, database, allowlist_path = no_code_api
    _seed_paper_repository(
        database,
        title="URL Must Not Be Silently Suppressed",
        domains=["agent"],
    )
    if failure == "missing":
        allowlist_path.unlink()
    else:
        allowlist_path.write_text("agent: [\n", encoding="utf-8")

    response = client.get(
        "/api/v1/search",
        params={"group_by": "paper", "type": "paper"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "configuration"
    assert response.json()["title"] == "无代码审计配置不可用"
    assert "example.invalid" not in response.text
