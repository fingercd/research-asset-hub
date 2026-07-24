from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "service": "research-asset-hub-worker",
        "status": "idle",
        "timestamp": utc_timestamp(),
        "pid": os.getpid(),
        "queue_depth": 0,
    }
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research Asset Hub worker placeholder")
    parser.add_argument(
        "--heartbeat-path",
        type=Path,
        default=Path(os.getenv("HUB_WORKER_HEARTBEAT_PATH", "work/state/worker-heartbeat.json")),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("HUB_WORKER_HEARTBEAT_INTERVAL_SECONDS", "5")),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Write one heartbeat and exit (useful for diagnostics).",
    )
    return parser


def run() -> None:
    arguments = build_parser().parse_args()
    if arguments.interval <= 0:
        raise SystemExit("--interval must be greater than zero")

    try:
        while True:
            write_heartbeat(arguments.heartbeat_path)
            if arguments.once:
                return
            time.sleep(arguments.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    run()
