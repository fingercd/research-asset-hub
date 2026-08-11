"""Build GitNexus indexes for registered local code representations.

The script is read-only with respect to the database and the source files
themselves. It only creates a ``.gitnexus/`` index directory inside each
resolved repository, which is the documented exception to the Hub's strict
read-only source policy. ``--dry-run`` (the default) prints the repository
plan; ``--apply`` runs ``gitnexus analyze --index-only`` per unique path.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings, get_settings
from apps.api.database import create_database_engine
from apps.api.editor import CODE_REPRESENTATION_TYPES, resolve_editor_path
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Representation, Source


DEFAULT_REPORT = Path("work/gitnexus/report.json")


@dataclass(slots=True)
class RepositoryPlan:
    path: str
    source_ids: list[str] = field(default_factory=list)
    representation_ids: list[str] = field(default_factory=list)
    status: str = "ready"
    duration_seconds: float | None = None
    error: str | None = None


@dataclass(slots=True)
class GitNexusReport:
    dry_run: bool
    repositories_total: int = 0
    repositories_skipped: int = 0
    repositories_indexed: int = 0
    repositories_failed: int = 0
    plans: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _collect_plans(
    session: Session,
    registry: SourceRegistry,
    *,
    source_alias: str | None,
    limit: int | None,
) -> tuple[list[RepositoryPlan], list[dict[str, Any]]]:
    statement = (
        select(Representation, Source)
        .join(Source, Source.id == Representation.source_id)
        .where(
            Representation.deleted_at.is_(None),
            Representation.representation_type.in_(CODE_REPRESENTATION_TYPES),
        )
        .order_by(Source.alias, Representation.id)
    )
    if source_alias is not None:
        statement = statement.where(Source.alias == source_alias)
    rows = session.execute(statement).all()
    if limit is not None:
        rows = rows[: max(0, limit)]

    plans: dict[str, RepositoryPlan] = {}
    rejected: list[dict[str, Any]] = []
    for representation, source in rows:
        source_id = str(source.alias)
        resolution = resolve_editor_path(
            registry,
            representation,
            source_alias=source_id,
        )
        if not resolution.available or not resolution.path:
            rejected.append(
                {
                    "source": source_id,
                    "representation_id": str(representation.id),
                    "outcome": "unavailable",
                    "reason": resolution.reason or "源码未下载/未开源",
                }
            )
            continue
        plan = plans.setdefault(
            resolution.path,
            RepositoryPlan(path=resolution.path),
        )
        if source_id not in plan.source_ids:
            plan.source_ids.append(source_id)
        plan.representation_ids.append(str(representation.id))
        if (Path(resolution.path) / ".gitnexus").is_dir():
            plan.status = "skipped"
    return list(plans.values()), rejected


def _run_gitnexus(plan: RepositoryPlan) -> tuple[bool, float, str]:
    executable = shutil.which("gitnexus")
    if executable is None:
        return False, 0.0, "未找到 gitnexus CLI，请先运行 npm install -g gitnexus"
    command = [executable, "analyze", "--index-only", plan.path]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, time.monotonic() - started, f"{type(error).__name__}: {error}"
    duration = time.monotonic() - started
    if completed.returncode == 0:
        return True, duration, ""
    detail = (completed.stderr or completed.stdout).strip() or "gitnexus analyze failed"
    return False, duration, detail[-4000:]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="run gitnexus analyze")
    parser.add_argument("--source")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel gitnexus analyze processes (default 1)",
    )
    parser.add_argument("--database-url")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="JSON report path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings: ApiSettings = (
        ApiSettings(database_url=arguments.database_url)
        if arguments.database_url
        else get_settings()
    )
    registry = SourceRegistry.load(settings.sources_config_path)
    engine = create_database_engine(settings)
    try:
        with Session(engine) as session:
            plans, rejected = _collect_plans(
                session,
                registry,
                source_alias=arguments.source,
                limit=arguments.limit,
            )
    finally:
        engine.dispose()

    report = GitNexusReport(dry_run=not arguments.apply)
    report.repositories_total = len(plans)
    if not arguments.apply:
        for plan in plans:
            if plan.status == "skipped":
                report.repositories_skipped += 1
            report.plans.append(asdict(plan))
        report.plans.extend(rejected)
        serialized = json.dumps(
            report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
        )
        if arguments.report is not None:
            arguments.report.parent.mkdir(parents=True, exist_ok=True)
            arguments.report.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0

    ready = [plan for plan in plans if plan.status != "skipped"]
    report.repositories_skipped = len(plans) - len(ready)
    for plan in plans:
        if plan.status == "skipped":
            report.plans.append(asdict(plan))

    def run_one(plan: RepositoryPlan) -> RepositoryPlan:
        print(
            f"[gitnexus] indexing {plan.path} ...",
            flush=True,
        )
        success, duration, error = _run_gitnexus(plan)
        plan.duration_seconds = duration
        plan.status = "indexed" if success else "failed"
        plan.error = error
        print(
            f"[gitnexus] {plan.status}: {plan.path} ({duration:.1f}s)",
            flush=True,
        )
        return plan

    workers = max(1, int(arguments.jobs))
    if workers == 1:
        completed = [run_one(plan) for plan in ready]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            completed = list(executor.map(run_one, ready))
    for plan in completed:
        if plan.status == "indexed":
            report.repositories_indexed += 1
        elif plan.status == "failed":
            report.repositories_failed += 1
        report.plans.append(asdict(plan))
    report.plans.extend(rejected)

    serialized = json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
