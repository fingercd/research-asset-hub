from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.api.config import ApiSettings, get_settings
from apps.api.database import DatabaseManager, DatabaseUnavailable
from apps.asset_gateway.registry import SourceRegistry
from apps.worker.handlers import RuntimeHandlers, dispatch_outbox_once
from apps.worker.lease import (
    ClaimedJob,
    claim_next_job,
    fail_job,
    finish_job,
    renew_lease,
)


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_heartbeat(
    path: Path,
    *,
    status: str,
    worker_id: str,
    current_job_id: str | None = None,
    detail: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "service": "research-asset-hub-worker",
        "status": status,
        "timestamp": utc_timestamp(),
        "pid": os.getpid(),
        "worker_id": worker_id,
        "current_job_id": current_job_id,
        "detail": detail,
    }
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


class LeaseHeartbeat:
    def __init__(
        self,
        database: DatabaseManager,
        claim: ClaimedJob,
        *,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        self.database = database
        self.claim = claim
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lost_lease = threading.Event()
        self.last_error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{claim.job_id}",
            daemon=True,
        )

    def __enter__(self) -> LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds + 1))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self.database.session() as session:
                    still_owned = renew_lease(
                        session,
                        self.claim,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                if not still_owned:
                    self._lost_lease.set()
                    return
                self.last_error = None
            except Exception as error:
                # A transient database restart must not permanently disable lease
                # renewal for a long extraction. Keep retrying until ownership is
                # explicitly lost or the handler finishes.
                self.last_error = error


class WorkerRuntime:
    def __init__(
        self,
        settings: ApiSettings,
        *,
        database: DatabaseManager | None = None,
        registry: SourceRegistry | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.database = database or DatabaseManager(settings)
        self.registry = registry or SourceRegistry.load(settings.sources_config_path)
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{os.urandom(4).hex()}"
        )
        self.handlers = RuntimeHandlers(self.database, settings, self.registry)

    def tick(self) -> bool:
        dispatch_outbox_once(self.database, worker_id=self.worker_id)
        with self.database.session() as session:
            claim = claim_next_job(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_seconds,
            )
        if claim is None:
            return False
        write_heartbeat(
            self.settings.worker_heartbeat_path,
            status="running",
            worker_id=self.worker_id,
            current_job_id=str(claim.job_id),
        )
        try:
            with LeaseHeartbeat(
                self.database,
                claim,
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_seconds,
                interval_seconds=self.settings.worker_heartbeat_seconds,
            ):
                self.handlers.dispatch(
                    claim.job_type,
                    claim.parameters,
                    job_id=claim.job_id,
                )
            with self.database.session() as session:
                finish_job(session, claim, worker_id=self.worker_id)
        except Exception as error:
            with self.database.session() as session:
                fail_job(
                    session,
                    claim,
                    worker_id=self.worker_id,
                    error=error,
                )
            write_heartbeat(
                self.settings.worker_heartbeat_path,
                status="error",
                worker_id=self.worker_id,
                current_job_id=str(claim.job_id),
                detail=f"{type(error).__name__}: {' '.join(str(error).split())[:300]}",
            )
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research Asset Hub lease worker")
    parser.add_argument(
        "--heartbeat-path",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run at most one queue iteration and exit.",
    )
    return parser


def _settings_with_overrides(arguments: argparse.Namespace) -> ApiSettings:
    settings = get_settings()
    updates: dict[str, Any] = {}
    if arguments.heartbeat_path is not None:
        updates["worker_heartbeat_path"] = arguments.heartbeat_path
    if arguments.interval is not None:
        if arguments.interval <= 0:
            raise SystemExit("--interval must be greater than zero")
        updates["worker_poll_seconds"] = arguments.interval
    return settings.model_copy(update=updates)


def run() -> None:
    arguments = build_parser().parse_args()
    settings = _settings_with_overrides(arguments)
    runtime = WorkerRuntime(settings)
    try:
        while True:
            try:
                processed = runtime.tick()
                write_heartbeat(
                    settings.worker_heartbeat_path,
                    status="idle",
                    worker_id=runtime.worker_id,
                )
            except DatabaseUnavailable as error:
                processed = False
                write_heartbeat(
                    settings.worker_heartbeat_path,
                    status="degraded",
                    worker_id=runtime.worker_id,
                    detail=str(error),
                )
            except Exception as error:
                processed = False
                write_heartbeat(
                    settings.worker_heartbeat_path,
                    status="error",
                    worker_id=runtime.worker_id,
                    detail=f"{type(error).__name__}: worker iteration failed.",
                )
            if arguments.once:
                return
            if not processed:
                time.sleep(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        return
    finally:
        runtime.database.dispose()


if __name__ == "__main__":
    run()
