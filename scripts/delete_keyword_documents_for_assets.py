"""Delete keyword-index documents for an exact reviewed asset ID set."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from apps.api.config import PROJECT_ROOT, get_settings
from packages.domain.exporters.common import atomic_write_json


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        type=Path,
        default=PROJECT_ROOT / "config" / "domain-scope-exclusions.yaml",
    )
    parser.add_argument("--index", action="append", default=["code"])
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "vln-uav-index-delete.json",
    )
    return parser.parse_args()


def _wait(client: httpx.Client, task_uid: int) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_uid}")
        response.raise_for_status()
        raw_payload = response.json()
        if not isinstance(raw_payload, dict):
            raise TypeError("Meilisearch task response must be an object")
        payload: dict[str, Any] = raw_payload
        if payload.get("status") == "succeeded":
            return payload
        if payload.get("status") in {"failed", "canceled"}:
            raise RuntimeError(f"Meilisearch task {task_uid} did not succeed")
        time.sleep(0.1)
    raise TimeoutError(f"Meilisearch task {task_uid} timed out")


def main() -> int:
    arguments = _arguments()
    policy = yaml.safe_load(arguments.policy.read_text(encoding="utf-8")) or {}
    asset_ids = sorted(
        {
            str(asset_id)
            for rule in policy.get("rules", [])
            for asset_id in rule.get("asset_ids", [])
        }
    )
    if len(asset_ids) != 42:
        raise SystemExit("expected exactly 42 reviewed asset IDs")
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.meilisearch_api_key}"}
    filter_value = "asset_id IN [" + ",".join(json.dumps(item) for item in asset_ids) + "]"
    results: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=settings.meilisearch_url,
        headers=headers,
        timeout=30.0,
    ) as client:
        for index_name in dict.fromkeys(arguments.index):
            before = client.get(f"/indexes/{index_name}/stats")
            before.raise_for_status()
            response = client.post(
                f"/indexes/{index_name}/documents/delete",
                json={"filter": filter_value},
            )
            response.raise_for_status()
            task = _wait(client, int(response.json()["taskUid"]))
            after = client.get(f"/indexes/{index_name}/stats")
            after.raise_for_status()
            results.append(
                {
                    "index": index_name,
                    "before": int(before.json()["numberOfDocuments"]),
                    "after": int(after.json()["numberOfDocuments"]),
                    "deleted": int(task.get("details", {}).get("deletedDocuments", 0)),
                }
            )
    report = {
        "schema": "research-asset-hub.keyword-asset-delete/v1",
        "status": "passed",
        "asset_ids": len(asset_ids),
        "indexes": results,
    }
    atomic_write_json(arguments.report, report)
    print(json.dumps({**report, "report": str(arguments.report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
