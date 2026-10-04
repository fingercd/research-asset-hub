"""Safety-first, isolated PostgreSQL and search-index restore rehearsal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Protocol

import httpx
import psycopg

from packages.domain.exporters.common import atomic_write_json

from .manager import BackupManager
from .workflow import wsl_path

ACTIVE_PROJECT_NAME = "research-asset-hub"
ACTIVE_PORTS = frozenset({5432, 7700, 8765, 8766, 8767})
CORE_TABLES = (
    "sources",
    "assets",
    "representations",
    "source_records",
    "source_record_versions",
    "source_record_assets",
    "field_claims",
    "relations",
    "relation_evidence",
    "annotations",
    "annotation_revisions",
    "reading_states",
    "sync_conflicts",
    "audit_events",
    "backup_runs",
    "source_retirements",
    "source_retirement_record_maps",
    "source_retirement_representation_maps",
    "source_retirement_mount_maps",
    "source_retirement_successors",
    "source_retirement_rollbacks",
)
ServingScope = Literal["serving_active", "all"]


def _normalize_scope(value: str) -> ServingScope:
    if value == "serving_active":
        return "serving_active"
    if value == "all":
        return "all"
    raise ValueError("scope must be 'serving_active' or 'all'")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


PROJECT_NAME_PATTERN = re.compile(r"^rah-restore-[a-z0-9][a-z0-9-]{2,50}$")


class RestoreSafetyError(ValueError):
    """Raised before any Docker or Restic mutation when isolation is uncertain."""


@dataclass(frozen=True, slots=True)
class RestorePorts:
    postgres: int = 55432
    meilisearch: int = 57700

    def values(self) -> tuple[int, ...]:
        return (
            self.postgres,
            self.meilisearch,
        )


@dataclass(frozen=True, slots=True)
class RestorePlan:
    project_root: Path
    run_root: Path
    compose_project: str
    database_name: str = "research_asset_hub_restore"
    database_user: str = "research_hub_restore"
    database_password: str = "restore-only-local"
    meili_key: str = "restore-only-local-meili-key"
    ports: RestorePorts = field(default_factory=RestorePorts)

    @property
    def compose_path(self) -> Path:
        return self.run_root / "compose.restore.yaml"

    @property
    def restore_target(self) -> Path:
        return self.run_root / "restored"

    @property
    def report_path(self) -> Path:
        return self.run_root / "report.json"

    def validate(self, *, require_free_ports: bool = False) -> None:
        project_root = self.project_root.resolve()
        run_root = self.run_root.resolve()
        allowed_root = (project_root / "work" / "restore-tests").resolve()
        try:
            relative_run = run_root.relative_to(allowed_root)
        except ValueError as exc:
            raise RestoreSafetyError(
                "restore run root must stay inside work/restore-tests"
            ) from exc
        if not relative_run.parts:
            raise RestoreSafetyError("restore run root must be a child of work/restore-tests")
        if not PROJECT_NAME_PATTERN.fullmatch(self.compose_project):
            raise RestoreSafetyError("restore Compose project name is not safely namespaced")
        if self.compose_project == ACTIVE_PROJECT_NAME:
            raise RestoreSafetyError("restore project cannot use the active Compose project name")
        if not self.database_name.endswith("_restore"):
            raise RestoreSafetyError("restore database name must end with _restore")
        if self.database_name == "research_asset_hub":
            raise RestoreSafetyError("restore database cannot use the active database name")
        ports = self.ports.values()
        if len(set(ports)) != len(ports):
            raise RestoreSafetyError("restore ports must be distinct")
        if any(port in ACTIVE_PORTS for port in ports):
            raise RestoreSafetyError("restore ports overlap active platform ports")
        if any(port < 1024 or port > 65535 for port in ports):
            raise RestoreSafetyError("restore ports must be unprivileged TCP ports")
        if require_free_ports:
            occupied = [port for port in ports if not _port_is_free(port)]
            if occupied:
                raise RestoreSafetyError(f"restore ports are already occupied: {occupied}")


@dataclass(frozen=True, slots=True)
class RestoredLayout:
    project_root: Path
    manifest_path: Path
    dump_path: Path
    config_path: Path
    open_catalog_path: Path


@dataclass(frozen=True, slots=True)
class IndexRebuildContext:
    database_dsn: str
    meili_url: str
    meili_key: str


class IndexRebuilder(Protocol):
    def rebuild(self, context: IndexRebuildContext) -> Mapping[str, Any]: ...


class MetadataIndexRebuilder:
    """Rebuild the keyword asset index in the isolated services."""

    def __init__(
        self,
        *,
        batch_size: int = 64,
        scope: ServingScope = "serving_active",
    ) -> None:
        self.batch_size = batch_size
        self.scope = _normalize_scope(scope)

    def rebuild(self, context: IndexRebuildContext) -> Mapping[str, Any]:
        rows = self._load_assets(context.database_dsn)
        documents = [
            {
                "document_id": row["id"],
                "asset_id": row["id"],
                "kind": row["asset_type"],
                "title": row["title"],
                "text": "\n".join(
                    value
                    for value in (
                        row["title_zh"],
                        row["abstract"],
                        row["summary"],
                        " ".join(row["authors"]),
                    )
                    if value
                ),
                "metadata": {
                    "asset_type": row["asset_type"],
                    "year": row["year"],
                    "domains": row["domain_slugs"],
                    "tags": row["tags"],
                },
            }
            for row in rows
        ]
        meili = self._rebuild_meili(context, documents)
        probe = self._fixed_query_probe(context, documents)
        expected = len(documents)
        status = (
            "verified"
            if meili["document_count"] == expected and probe["keyword_exact_hit"]
            else "failed"
        )
        return {
            "status": status,
            "strategy": "metadata-keyword-rebuild",
            "expected_assets": expected,
            "meilisearch": meili,
            "fixed_query": probe,
            "production_note": (
                "Meilisearch keyword indexes are rebuildable state and are not restored."
            ),
            "scope": self.scope,
        }

    def _load_assets(self, database_dsn: str) -> list[dict[str, Any]]:
        serving_clause = (
            ""
            if self.scope == "all"
            else """
            AND (
                NOT EXISTS (
                    SELECT 1 FROM representations any_rep
                    WHERE any_rep.asset_id = assets.id
                      AND any_rep.deleted_at IS NULL
                )
                OR EXISTS (
                    SELECT 1 FROM representations active_rep
                    WHERE active_rep.asset_id = assets.id
                      AND active_rep.deleted_at IS NULL
                      AND (
                          active_rep.source_id IS NULL
                          OR NOT EXISTS (
                              SELECT 1 FROM source_retirements retirement
                              WHERE retirement.source_id = active_rep.source_id
                                AND NOT EXISTS (
                                    SELECT 1 FROM source_retirement_rollbacks rollback
                                    WHERE rollback.retirement_id = retirement.id
                                )
                          )
                      )
                )
            )
            """
        )
        sql = f"""
            SELECT
                id::text,
                title,
                COALESCE(title_zh, ''),
                asset_type,
                year,
                COALESCE(abstract, ''),
                COALESCE(summary, ''),
                authors,
                domain_slugs,
                tags
            FROM assets
            WHERE deleted_at IS NULL
            {serving_clause}
            ORDER BY id
        """
        with psycopg.connect(database_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                values = cursor.fetchall()
        return [
            {
                "id": str(row[0]),
                "title": str(row[1]),
                "title_zh": str(row[2]),
                "asset_type": str(row[3]),
                "year": row[4],
                "abstract": str(row[5]),
                "summary": str(row[6]),
                "authors": list(row[7] or []),
                "domain_slugs": list(row[8] or []),
                "tags": list(row[9] or []),
            }
            for row in values
        ]

    def _rebuild_meili(
        self,
        context: IndexRebuildContext,
        documents: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {context.meili_key}"}
        with httpx.Client(base_url=context.meili_url, headers=headers, timeout=30.0) as client:
            response = client.post(
                "/indexes",
                json={"uid": "assets", "primaryKey": "document_id"},
            )
            if response.status_code not in {200, 201, 202, 409}:
                response.raise_for_status()
            if response.status_code != 409:
                self._wait_meili_task(client, int(response.json()["taskUid"]))
            for offset in range(0, len(documents), self.batch_size):
                task = client.post(
                    "/indexes/assets/documents",
                    json=list(documents[offset : offset + self.batch_size]),
                )
                task.raise_for_status()
                self._wait_meili_task(client, int(task.json()["taskUid"]))
            stats = client.get("/indexes/assets/stats")
            stats.raise_for_status()
            return {
                "status": "verified",
                "index": "assets",
                "document_count": int(stats.json()["numberOfDocuments"]),
            }

    @staticmethod
    def _wait_meili_task(client: httpx.Client, task_id: int) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            response = client.get(f"/tasks/{task_id}")
            response.raise_for_status()
            payload = response.json()
            if payload["status"] == "succeeded":
                return
            if payload["status"] in {"failed", "canceled"}:
                raise RuntimeError(f"Meilisearch task {task_id} failed: {payload}")
            time.sleep(0.1)
        raise TimeoutError(f"Meilisearch task {task_id} did not finish")

    def _fixed_query_probe(
        self,
        context: IndexRebuildContext,
        documents: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not documents:
            return {
                "asset_id": None,
                "keyword_exact_hit": True,
            }
        document = documents[0]
        asset_id = str(document["asset_id"])
        title = str(document["title"])
        headers = {"Authorization": f"Bearer {context.meili_key}"}
        with httpx.Client(timeout=30.0) as client:
            keyword = client.post(
                f"{context.meili_url}/indexes/assets/search",
                headers=headers,
                json={"q": title, "limit": 5},
            )
            keyword.raise_for_status()
            keyword_ids = [str(hit["asset_id"]) for hit in keyword.json()["hits"]]
        return {
            "asset_id": asset_id,
            "query_title": title,
            "keyword_top5": keyword_ids,
            "keyword_exact_hit": asset_id in keyword_ids,
        }


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def render_restore_compose(plan: RestorePlan) -> str:
    plan.validate()
    meili_health = (
        "wget -q -O - "
        f"--header='Authorization: Bearer {plan.meili_key}' "
        "http://127.0.0.1:7700/health | grep -q available"
    )
    return f"""services:
  postgres:
    image: postgres:16-alpine
    # Keep the isolated rehearsal bounded just like the serving environment.
    mem_limit: 768m
    mem_reservation: 384m
    environment:
      POSTGRES_DB: {plan.database_name}
      POSTGRES_USER: {plan.database_user}
      POSTGRES_PASSWORD: {plan.database_password}
    ports:
      - "127.0.0.1:{plan.ports.postgres}:5432"
    volumes:
      - postgres-restore-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {plan.database_user} -d {plan.database_name}"]
      interval: 2s
      timeout: 3s
      retries: 30
    restart: "no"

  meilisearch:
    image: getmeili/meilisearch:v1.12
    mem_limit: 1536m
    mem_reservation: 512m
    environment:
      MEILI_ENV: development
      MEILI_NO_ANALYTICS: "true"
      MEILI_MASTER_KEY: {plan.meili_key}
    ports:
      - "127.0.0.1:{plan.ports.meilisearch}:7700"
    volumes:
      - meili-restore-data:/meili_data
    healthcheck:
      test: ["CMD-SHELL", {json.dumps(meili_health)}]
      interval: 2s
      timeout: 3s
      retries: 30
    restart: "no"

volumes:
  postgres-restore-data:
  meili-restore-data:
"""


def discover_restored_layout(target: Path) -> RestoredLayout:
    manifests = []
    for candidate in target.rglob("manifest.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("schema", "")).startswith("research-asset-hub.backup/"):
            manifests.append(candidate)
    if len(manifests) != 1:
        raise RuntimeError(f"expected one platform backup manifest, found {len(manifests)}")
    manifest_path = manifests[0]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload["schema"] == "research-asset-hub.backup/v3":
        project_root = manifest_path.parent
    elif payload["schema"] == "research-asset-hub.backup/v2":
        if len(manifest_path.parents) < 4:
            raise RuntimeError("restored backup manifest does not preserve the project layout")
        project_root = manifest_path.parents[3]
    else:
        raise RuntimeError("unsupported platform backup schema")
    dump_path = manifest_path.parent / "postgres.dump"
    config_path = project_root / "config"
    open_catalog_path = project_root / "outputs" / "open-catalog"
    required: tuple[Path, ...] = (dump_path, config_path, open_catalog_path)
    if payload["schema"] == "research-asset-hub.backup/v3":
        required += (project_root / "library",)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"restored snapshot is incomplete: {missing}")
    return RestoredLayout(
        project_root=project_root,
        manifest_path=manifest_path,
        dump_path=dump_path,
        config_path=config_path,
        open_catalog_path=open_catalog_path,
    )


def verify_restored_manifest(layout: RestoredLayout) -> dict[str, Any]:
    payload = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    raw_files = payload.get("files")
    if not isinstance(raw_files, Mapping):
        raise RuntimeError("backup manifest does not contain a file inventory")
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for relative_path, expected_raw in raw_files.items():
        if not isinstance(relative_path, str) or not isinstance(expected_raw, Mapping):
            mismatches.append({"path": str(relative_path), "reason": "invalid manifest entry"})
            continue
        parts = relative_path.replace("\\", "/").split("/")
        if (
            PureWindowsPath(relative_path).drive
            or any(part in {"", ".", ".."} for part in parts)
            or "\0" in relative_path
        ):
            mismatches.append({"path": relative_path, "reason": "unsafe-path"})
            continue
        candidate = layout.project_root / Path(relative_path)
        if not candidate.resolve().is_relative_to(layout.project_root.resolve()):
            mismatches.append({"path": relative_path, "reason": "unsafe-path"})
            continue
        if not candidate.is_file():
            mismatches.append({"path": relative_path, "reason": "missing"})
            continue
        checked += 1
        actual_size = candidate.stat().st_size
        actual_hash = _sha256(candidate)
        if actual_size != expected_raw.get("size", -1) or actual_hash != str(
            expected_raw.get("sha256", "")
        ):
            mismatches.append(
                {
                    "path": relative_path,
                    "reason": "digest-mismatch",
                    "actual_size": actual_size,
                    "actual_sha256": actual_hash,
                }
            )
    if payload.get("schema") == "research-asset-hub.backup/v3":
        actual_files = {
            path.relative_to(layout.project_root).as_posix()
            for path in layout.project_root.rglob("*")
            if path.is_file() and path != layout.manifest_path
        }
        for path in sorted(actual_files - set(raw_files)):
            mismatches.append({"path": path, "reason": "unlisted-file"})
        if "postgres.dump" not in raw_files:
            mismatches.append({"path": "postgres.dump", "reason": "missing-inventory"})
    return {
        "status": "verified" if not mismatches else "failed",
        "schema": payload.get("schema"),
        "files_checked": checked,
        "mismatches": mismatches,
        "config_files": sum(1 for path in raw_files if str(path).startswith("config/")),
        "open_catalog_files": sum(
            1 for path in raw_files if str(path).startswith("outputs/open-catalog/")
        ),
        "managed_library_files": sum(1 for path in raw_files if str(path).startswith("library/")),
        "excluded_external_originals": payload.get("excluded_external_originals"),
        "tree_digests": payload.get("tree_digests", {}),
        "data_scope": payload.get("data_scope"),
        "retirement_history_included": payload.get("retirement_history_included"),
    }


def compare_database_snapshots(
    baseline: Mapping[str, Any],
    restored: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_counts = baseline.get("counts", {})
    restored_counts = restored.get("counts", {})
    count_differences = {
        table: {
            "baseline": baseline_counts.get(table),
            "restored": restored_counts.get(table),
        }
        for table in CORE_TABLES
        if baseline_counts.get(table) != restored_counts.get(table)
    }
    fixed_query_equal = baseline.get("fixed_queries") == restored.get("fixed_queries")
    retirement_ledger_equal = _mapping(baseline.get("fixed_queries")).get(
        "retirement_ledger"
    ) == _mapping(restored.get("fixed_queries")).get("retirement_ledger")
    revision_equal = baseline.get("schema_revision") == restored.get("schema_revision")
    invariants = restored.get("invariants", {})
    invariants_valid = all(value == 0 for value in invariants.values())
    return {
        "status": (
            "verified"
            if not count_differences
            and fixed_query_equal
            and retirement_ledger_equal
            and revision_equal
            and invariants_valid
            else "failed"
        ),
        "count_differences": count_differences,
        "fixed_queries_equal": fixed_query_equal,
        "retirement_ledger_equal": retirement_ledger_equal,
        "schema_revision_equal": revision_equal,
        "restored_invariants": invariants,
    }


def run_restore_rehearsal(
    plan: RestorePlan,
    *,
    snapshot_id: str,
    manager: BackupManager | None = None,
    index_rebuilder: IndexRebuilder | None = None,
    dry_run: bool = False,
    keep_environment: bool = False,
    scope: ServingScope = "serving_active",
) -> dict[str, Any]:
    """Restore into isolated resources, validate, report, and safely tear them down."""

    normalized_scope = _normalize_scope(scope)
    plan.validate(require_free_ports=not dry_run)
    plan.run_root.mkdir(parents=True, exist_ok=False)
    compose = render_restore_compose(plan)
    plan.compose_path.write_text(compose, encoding="utf-8", newline="\n")
    report: dict[str, Any] = {
        "schema": "research-asset-hub.restore-rehearsal/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "planned" if dry_run else "running",
        "dry_run": dry_run,
        "snapshot_id": snapshot_id,
        "scope": normalized_scope,
        "safety": {
            "active_project": ACTIVE_PROJECT_NAME,
            "restore_project": plan.compose_project,
            "restore_database": plan.database_name,
            "restore_ports": asdict(plan.ports),
            "loopback_only": True,
            "active_resources_mutated": False,
        },
        "artifacts": {
            "compose": str(plan.compose_path),
            "report": str(plan.report_path),
        },
        "phases": [],
    }
    if dry_run:
        report["status"] = "dry-run-verified"
        report["phases"] = ["safety-plan", "compose-render"]
        report["finished_at"] = datetime.now(UTC).isoformat()
        atomic_write_json(plan.report_path, report)
        return report

    manager = manager or BackupManager()
    index_rebuilder = index_rebuilder or MetadataIndexRebuilder(scope=normalized_scope)
    compose_started = False
    try:
        baseline = _query_active_database(plan.project_root)
        report["phases"].append("active-database-read-only-baseline")

        manager.restore(
            snapshot_id,
            plan.restore_target,
            allowed_root=(plan.project_root / "work" / "restore-tests"),
        )
        layout = discover_restored_layout(plan.restore_target)
        manifest_validation = verify_restored_manifest(layout)
        if manifest_validation["status"] != "verified":
            raise RuntimeError("restored file manifest verification failed")
        report["manifest_validation"] = manifest_validation
        report["artifacts"].update(
            {
                "restored_project": str(layout.project_root),
                "postgres_dump": str(layout.dump_path),
                "configuration": str(layout.config_path),
                "open_catalog": str(layout.open_catalog_path),
            }
        )
        report["phases"].append("restic-files-restored-and-verified")

        compose_started = True
        _compose(plan, ["up", "-d", "--wait"])
        runtime_isolation = _verify_runtime_isolation(plan)
        report["runtime_isolation"] = runtime_isolation
        report["phases"].append("isolated-compose-started")

        _compose(plan, ["cp", wsl_path(layout.dump_path), "postgres:/tmp/postgres.dump"])
        _compose(
            plan,
            [
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "-U",
                plan.database_user,
                "-d",
                plan.database_name,
                "/tmp/postgres.dump",
            ],
        )
        restored = _query_restore_database(plan)
        database_validation = compare_database_snapshots(baseline, restored)
        report["database"] = {
            "baseline": baseline,
            "restored": restored,
            "validation": database_validation,
        }
        if database_validation["status"] != "verified":
            raise RuntimeError("restored PostgreSQL validation failed")
        report["phases"].append("postgres-restored-and-verified")

        context = IndexRebuildContext(
            database_dsn=(
                f"postgresql://{plan.database_user}:{plan.database_password}"
                f"@127.0.0.1:{plan.ports.postgres}/{plan.database_name}"
            ),
            meili_url=f"http://127.0.0.1:{plan.ports.meilisearch}",
            meili_key=plan.meili_key,
        )
        index_result = dict(index_rebuilder.rebuild(context))
        report["index_rebuild"] = index_result
        if index_result.get("status") != "verified":
            raise RuntimeError("isolated search index rebuild failed")
        report["phases"].append("indexes-rebuilt-and-verified")
        report["status"] = "verified"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        raise
    finally:
        cleanup: dict[str, Any]
        if compose_started and not keep_environment:
            try:
                _compose(plan, ["down", "--volumes", "--remove-orphans"])
                cleanup = {
                    "status": "verified",
                    "environment_removed": True,
                    "project": plan.compose_project,
                }
            except Exception as exc:
                cleanup = {
                    "status": "failed",
                    "environment_removed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                report["status"] = "failed"
        else:
            cleanup = {
                "status": "retained" if compose_started else "not-started",
                "environment_removed": False,
            }
        report["cleanup"] = cleanup
        report["finished_at"] = datetime.now(UTC).isoformat()
        atomic_write_json(plan.report_path, report)
    return report


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _compose(plan: RestorePlan, arguments: Sequence[str]) -> str:
    compose_path = shlex.quote(wsl_path(plan.compose_path))
    project = shlex.quote(plan.compose_project)
    command = (
        "DOCKER_CONFIG=/tmp/rah-docker-config "
        f"docker compose -p {project} -f {compose_path} "
        + " ".join(shlex.quote(argument) for argument in arguments)
    )
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu", "-u", "root", "--", "sh", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"isolated Compose command failed: {message}")
    return result.stdout.strip()


def _verify_runtime_isolation(plan: RestorePlan) -> dict[str, Any]:
    raw_ids = _compose(plan, ["ps", "-q"])
    container_ids = [value for value in raw_ids.splitlines() if value]
    # The restore compose intentionally contains only the two stateful
    # services.  (The default Compose network is not a container.)
    expected_container_count = 2
    if len(container_ids) != expected_container_count:
        raise RestoreSafetyError(
            "isolated Compose project should have "
            f"{expected_container_count} containers, found {len(container_ids)}"
        )
    command = "docker inspect " + " ".join(
        shlex.quote(container_id) for container_id in container_ids
    )
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu", "-u", "root", "--", "sh", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker inspect failed: {result.stderr.strip()}")
    inspected = json.loads(result.stdout)
    volume_names: list[str] = []
    for container in inspected:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != plan.compose_project:
            raise RestoreSafetyError("container escaped the restore Compose project")
        for mount in container.get("Mounts", []):
            if mount.get("Type") != "volume":
                continue
            name = str(mount.get("Name", ""))
            if not name.startswith(f"{plan.compose_project}_"):
                raise RestoreSafetyError(f"non-isolated volume detected: {name}")
            volume_names.append(name)
    return {
        "status": "verified",
        "container_count": len(container_ids),
        "compose_project_labels": [plan.compose_project],
        "volume_names": sorted(volume_names),
        "all_volumes_namespaced": True,
    }


def _database_snapshot_sql() -> str:
    counts = ", ".join(f"'{table}', (SELECT count(*) FROM {table})" for table in CORE_TABLES)
    return f"""
        SELECT jsonb_build_object(
            'schema_revision', (SELECT version_num FROM alembic_version),
            'counts', jsonb_build_object({counts}),
            'invariants', jsonb_build_object(
                'dangling_relations',
                (
                    SELECT count(*)
                    FROM relations r
                    LEFT JOIN assets s ON s.id = r.subject_asset_id
                    LEFT JOIN assets o ON o.id = r.object_asset_id
                    WHERE s.id IS NULL OR o.id IS NULL
                ),
                'duplicate_logical_relations',
                (
                    SELECT count(*)
                    FROM (
                        SELECT subject_asset_id, relation_type, object_asset_id
                        FROM relations
                        WHERE deleted_at IS NULL
                        GROUP BY subject_asset_id, relation_type, object_asset_id
                        HAVING count(*) > 1
                    ) duplicates
                ),
                'active_retirement_enabled_sources',
                (
                    SELECT count(*)
                    FROM source_retirements retirement
                    JOIN sources source ON source.id = retirement.source_id
                    WHERE source.enabled
                      AND NOT EXISTS (
                          SELECT 1 FROM source_retirement_rollbacks rollback
                          WHERE rollback.retirement_id = retirement.id
                      )
                ),
                'active_retirement_incomplete_ledger',
                (
                    SELECT count(*)
                    FROM source_retirements retirement
                    WHERE NOT EXISTS (
                        SELECT 1 FROM source_retirement_rollbacks rollback
                        WHERE rollback.retirement_id = retirement.id
                    )
                      AND (
                        COALESCE(jsonb_array_length(retirement.payload->'record_mappings'), -1)
                            <> (SELECT count(*) FROM source_retirement_record_maps item
                                WHERE item.retirement_id = retirement.id)
                        OR COALESCE(
                            jsonb_array_length(
                                retirement.payload->'representation_mappings'
                            ),
                            -1
                        )
                            <> (SELECT count(*) FROM source_retirement_representation_maps item
                                WHERE item.retirement_id = retirement.id)
                        OR COALESCE(jsonb_array_length(retirement.payload->'mount_mappings'), -1)
                            <> (SELECT count(*) FROM source_retirement_mount_maps item
                                WHERE item.retirement_id = retirement.id)
                        OR COALESCE(jsonb_array_length(retirement.payload->'successors'), -1)
                            <> (SELECT count(*) FROM source_retirement_successors item
                                WHERE item.retirement_id = retirement.id)
                      )
                )
            ),
            'fixed_queries', jsonb_build_object(
                'asset_sample',
                (
                    SELECT COALESCE(jsonb_agg(to_jsonb(sample)), '[]'::jsonb)
                    FROM (
                        SELECT id::text AS id, title, asset_type, year
                        FROM assets
                        WHERE deleted_at IS NULL
                        ORDER BY id
                        LIMIT 10
                    ) sample
                ),
                'domain_distribution',
                (
                    SELECT COALESCE(jsonb_object_agg(domain, count), '{{}}'::jsonb)
                    FROM (
                        SELECT domain, count(*) AS count
                        FROM assets, LATERAL jsonb_array_elements_text(domain_slugs) AS domain
                        WHERE deleted_at IS NULL
                        GROUP BY domain
                        ORDER BY domain
                    ) values
                ),
                'reading_state_distribution',
                (
                    SELECT COALESCE(jsonb_object_agg(status, count), '{{}}'::jsonb)
                    FROM (
                        SELECT status, count(*) AS count
                        FROM reading_states
                        GROUP BY status
                        ORDER BY status
                    ) values
                ),
                'retirement_ledger',
                (
                    SELECT COALESCE(jsonb_agg(to_jsonb(ledger) ORDER BY ledger.id), '[]'::jsonb)
                    FROM (
                        SELECT
                            retirement.id::text AS id,
                            retirement.source_id::text AS source_id,
                            retirement.backup_id::text AS backup_id,
                            retirement.payload_digest,
                            retirement.collection_digest,
                            retirement.signature_algorithm,
                            retirement.signer_key_id,
                            md5(retirement.signature) AS signature_fingerprint,
                            (rollback.id IS NULL) AS active,
                            (SELECT count(*) FROM source_retirement_record_maps item
                                WHERE item.retirement_id = retirement.id) AS record_maps,
                            (SELECT count(*) FROM source_retirement_representation_maps item
                                WHERE item.retirement_id = retirement.id) AS representation_maps,
                            (SELECT count(*) FROM source_retirement_mount_maps item
                                WHERE item.retirement_id = retirement.id) AS mount_maps,
                            (SELECT count(*) FROM source_retirement_successors item
                                WHERE item.retirement_id = retirement.id) AS successors
                        FROM source_retirements retirement
                        LEFT JOIN source_retirement_rollbacks rollback
                            ON rollback.retirement_id = retirement.id
                    ) ledger
                )
            )
        )::text;
    """


def _query_active_database(project_root: Path) -> dict[str, Any]:
    compose_path = shlex.quote(wsl_path(project_root / "deploy" / "compose.yaml"))
    sql = shlex.quote(_database_snapshot_sql())
    command = (
        f"cd {shlex.quote(wsl_path(project_root))} && "
        "DOCKER_CONFIG=/tmp/rah-docker-config "
        f"docker compose -f {compose_path} exec -T postgres "
        f"psql -X -A -t -U research_hub -d research_asset_hub -c {sql}"
    )
    try:
        return _run_json_wsl(command)
    except RuntimeError as compose_error:
        # The packaged desktop runs PostgreSQL natively, so there may be no
        # active Compose service even though the local database is healthy.
        # Preserve the read-only baseline check by querying that database
        # directly before starting the isolated restore containers.
        database_dsn = os.environ.get(
            "HUB_DATABASE_URL",
            "postgresql://research_hub:research_hub_local@127.0.0.1:5432/research_asset_hub",
        ).replace("postgresql+psycopg://", "postgresql://", 1)
        try:
            with psycopg.connect(database_dsn) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_database_snapshot_sql())
                    row = cursor.fetchone()
        except Exception as native_error:
            raise RuntimeError(
                "read-only database validation failed through Compose and "
                f"the native desktop database: {type(native_error).__name__}"
            ) from compose_error
        if row is None:
            raise RuntimeError("native database validation query returned no rows") from None
        value = json.loads(str(row[0]))
        if not isinstance(value, dict):
            raise TypeError("database validation query did not return an object") from None
        return value


def _query_restore_database(plan: RestorePlan) -> dict[str, Any]:
    sql = _database_snapshot_sql()
    output = _compose(
        plan,
        [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-A",
            "-t",
            "-U",
            plan.database_user,
            "-d",
            plan.database_name,
            "-c",
            sql,
        ],
    )
    value = json.loads(output)
    if not isinstance(value, dict):
        raise TypeError("restored database validation query did not return an object")
    return value


def _run_json_wsl(command: str) -> dict[str, Any]:
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu", "-u", "root", "--", "sh", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"read-only database validation failed: {message}")
    value = json.loads(result.stdout.strip())
    if not isinstance(value, dict):
        raise TypeError("database validation query did not return an object")
    return value
