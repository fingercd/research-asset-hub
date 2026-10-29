"""Preview and safely apply the configured Obsidian three-way synchronization."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.api.config import ApiSettings, get_settings
from apps.api.database import DatabaseManager
from packages.domain.exporters.common import atomic_write_json
from packages.integrations import (
    obsidian_sync_etag,
    preview_obsidian_assets,
    sync_obsidian_assets,
)

REPORT_SCHEMA = "research-asset-hub.obsidian-sync-report/v1"
_PREVIEW_COUNTERS = (
    "assets_seen",
    "create",
    "update_external",
    "update_hub",
    "unchanged",
    "conflicts",
)
_APPLY_COUNTERS = (
    "assets_seen",
    "created",
    "updated",
    "unchanged",
    "hub_updates",
    "conflicts",
)


class SyncPreviewStaleError(RuntimeError):
    """Raised when either side changed between preview and apply."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview the configured Obsidian sync, verify its ETag, then apply it "
            "without overwriting conflicts."
        )
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Persist and report the preview without writing Obsidian or Hub fields.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write a metadata-only JSON report (no note or annotation bodies).",
    )
    return parser


def _safe_counters(
    value: object,
    allowed: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counters: dict[str, int] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            counters[key] = item
    return counters


def _safe_run(
    result: Mapping[str, Any],
    *,
    counter_names: tuple[str, ...],
) -> dict[str, Any]:
    """Whitelist operational metadata so reports can never contain private text."""

    return {
        "status": str(result.get("status") or "unknown"),
        "sync_run_id": str(result.get("sync_run_id") or ""),
        "counters": _safe_counters(result.get("counters"), counter_names),
    }


def _report(
    preview: Mapping[str, Any],
    *,
    applied: Mapping[str, Any] | None,
) -> dict[str, Any]:
    preview_report = _safe_run(preview, counter_names=_PREVIEW_COUNTERS)
    preview_report["etag"] = str(preview.get("etag") or "")
    return {
        "schema": REPORT_SCHEMA,
        "provider": "obsidian",
        "mode": "apply" if applied is not None else "preview",
        "status": (
            str(applied.get("status") or "unknown")
            if applied is not None
            else str(preview.get("status") or "unknown")
        ),
        "preview_only": applied is None,
        "preview": preview_report,
        "apply": (
            _safe_run(applied, counter_names=_APPLY_COUNTERS) if applied is not None else None
        ),
    }


def run_sync(
    settings: ApiSettings,
    *,
    preview_only: bool,
    database: DatabaseManager | None = None,
) -> dict[str, Any]:
    """Preview, verify and optionally apply using one database transaction."""

    manager = database or DatabaseManager(settings)
    vault_path = settings.obsidian_vault_path
    hub_directory = settings.obsidian_hub_directory

    # One managed session means the preview records, ETag check and apply either
    # commit together or roll back together.
    with manager.session() as session:
        preview = preview_obsidian_assets(
            session,
            vault_path,
            hub_directory=hub_directory,
        )
        if preview_only:
            return _report(preview, applied=None)

        expected_etag = str(preview.get("etag") or "")
        current_etag = obsidian_sync_etag(
            session,
            vault_path,
            hub_directory=hub_directory,
        )
        if not expected_etag or current_etag != expected_etag:
            raise SyncPreviewStaleError(
                "Hub or Obsidian content changed after preview; no changes were applied."
            )

        applied = sync_obsidian_assets(
            session,
            vault_path,
            hub_directory=hub_directory,
        )
        return _report(preview, applied=applied)


def _stale_report() -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "provider": "obsidian",
        "mode": "apply",
        "status": "stale",
        "preview_only": True,
        "error": {
            "code": "sync_preview_stale",
            "detail": "Hub or Obsidian content changed; run preview again.",
        },
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: ApiSettings | None = None,
    database: DatabaseManager | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    active_settings = settings or get_settings()
    try:
        report = run_sync(
            active_settings,
            preview_only=arguments.preview_only,
            database=database,
        )
        exit_code = 0
    except SyncPreviewStaleError:
        report = _stale_report()
        exit_code = 2

    if arguments.report is not None:
        atomic_write_json(arguments.report.expanduser().resolve(), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
