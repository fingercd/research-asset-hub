"""Run an isolated local restore rehearsal and emit a machine-readable report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from packages.backup import (
    BackupManager,
    RestorePlan,
    RestorePorts,
    run_restore_rehearsal,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="latest", help="Restic snapshot ID")
    parser.add_argument("--run-id", help="Safe suffix for the isolated Compose project")
    parser.add_argument("--dry-run", action="store_true", help="Validate/render without restoring")
    parser.add_argument(
        "--keep-environment",
        action="store_true",
        help="Keep the isolated containers and volumes after validation",
    )
    parser.add_argument("--postgres-port", type=int, default=55432)
    parser.add_argument("--meili-port", type=int, default=57700)
    parser.add_argument("--qdrant-http-port", type=int, default=56333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=56334)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    project_root = Path(__file__).resolve().parents[1]
    run_id = arguments.run_id or datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz")
    run_id = run_id.casefold().replace("_", "-")
    compose_project = f"rah-restore-{run_id}"
    run_root = project_root / "work" / "restore-tests" / run_id
    plan = RestorePlan(
        project_root=project_root,
        run_root=run_root,
        compose_project=compose_project,
        ports=RestorePorts(
            postgres=arguments.postgres_port,
            meilisearch=arguments.meili_port,
            qdrant_http=arguments.qdrant_http_port,
            qdrant_grpc=arguments.qdrant_grpc_port,
        ),
    )
    manager: BackupManager | None = None
    snapshot = arguments.snapshot
    if not arguments.dry_run:
        manager = BackupManager()
        if snapshot == "latest":
            snapshot = manager.latest_snapshot()
    try:
        report = run_restore_rehearsal(
            plan,
            snapshot_id=snapshot,
            manager=manager,
            dry_run=arguments.dry_run,
            keep_environment=arguments.keep_environment,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "report": str(plan.report_path),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "snapshot_id": snapshot,
                "report": str(plan.report_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
