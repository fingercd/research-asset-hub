"""Hard-delete the reviewed UAV-VLN assets and their unshared local material."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.exporters.common import atomic_write_json
from packages.domain.models import Asset, Relation, Representation, Source
from packages.domain.scope_policy import DomainScopePolicy

DOMAIN = "vision-language-navigation"
EXPECTED_PAPERS = 31
EXPECTED_REPOSITORIES = 11
GENERATED_ROOT = PROJECT_ROOT.parent / "个人新增资料" / "2026-09-04-vln-unified-guides"
EXTRACTION_ROOT = PROJECT_ROOT / "work" / "mineru" / "vln-20260904" / "extracted"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--retry-file-failures", action="store_true")
    parser.add_argument(
        "--policy",
        type=Path,
        default=PROJECT_ROOT / "config" / "domain-scope-exclusions.yaml",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "vln-uav-hard-delete.json",
    )
    return parser.parse_args()


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.resolve() != root.resolve()


def _minimal_paths(paths: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in sorted({item.resolve() for item in paths}, key=lambda item: len(item.parts)):
        if any(_contained(path, parent) for parent in selected):
            continue
        selected.append(path)
    return selected


def _select_delete_target(
    *,
    registry: SourceRegistry,
    source_alias: str,
    representation: Representation,
) -> Path | None:
    if representation.path_ref is None:
        return None
    root = registry.root_for(source_alias, representation.root_key).resolve()
    path = registry.resolve(
        source_alias,
        representation.root_key,
        representation.path_ref,
    ).resolve()
    if not path.exists() or not _contained(path, root):
        return None
    if path.is_dir():
        return path
    if representation.representation_type in {"pdf", "guide"}:
        parent = path.parent.resolve()
        if _contained(parent, root):
            return parent
    return path


def _remove_readonly(
    function: Any,
    path: str,
    _error: tuple[type[BaseException], BaseException, Any],
) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _remove_path(path: Path) -> str:
    if path.is_dir():
        shutil.rmtree(path, onerror=_remove_readonly)
        return "directory"
    path.unlink()
    return "file"


def retry_file_failures(report_path: Path) -> dict[str, Any]:
    raw_report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(raw_report, dict):
        raise TypeError("hard-delete report must be a JSON object")
    report: dict[str, Any] = raw_report
    registry = SourceRegistry.load(get_settings().sources_config_path)
    allowed_roots = [item.path.resolve() for item in registry.describe()]
    failures: list[dict[str, str]] = []
    removed = list(report.get("files_removed", []))
    for raw_failure in report.get("file_failures", []):
        path = Path(str(raw_failure.get("path") or "")).resolve()
        if not any(_contained(path, root) for root in allowed_roots):
            raise RuntimeError(f"retry target is outside registered source roots: {path}")
        if not path.exists():
            continue
        try:
            removed.append({"path": str(path), "kind": _remove_path(path)})
        except Exception as error:
            failures.append(
                {
                    "path": str(path),
                    "error_type": type(error).__name__,
                    "message": str(error)[:300],
                }
            )
    report["status"] = "passed" if not failures else "partial"
    report["files_removed"] = removed
    report["file_failures"] = failures
    return report


def run_delete(*, policy_path: Path, execute: bool) -> dict[str, Any]:
    settings = get_settings()
    database = DatabaseManager(settings)
    registry = SourceRegistry.load(settings.sources_config_path)
    policy = DomainScopePolicy.load(policy_path)
    target_ids = {UUID(item) for rule in policy.rules for item in rule.asset_ids}
    if len(target_ids) != EXPECTED_PAPERS + EXPECTED_REPOSITORIES:
        raise RuntimeError("reviewed deletion policy must contain exactly 42 asset IDs")
    try:
        with database.session() as session:
            assets = list(session.scalars(select(Asset).where(Asset.id.in_(target_ids))).all())
            if len(assets) != len(target_ids):
                found = {item.id for item in assets}
                missing_ids = sorted(str(item) for item in target_ids - found)
                raise RuntimeError(
                    f"reviewed deletion assets are missing: {missing_ids}"
                )
            papers = [item for item in assets if item.asset_type == "paper"]
            repositories = [item for item in assets if item.asset_type == "repository"]
            if len(papers) != EXPECTED_PAPERS or len(repositories) != EXPECTED_REPOSITORIES:
                raise RuntimeError("reviewed deletion set has an unexpected asset-type split")
            representations = list(
                session.scalars(
                    select(Representation).where(Representation.asset_id.in_(target_ids))
                ).all()
            )
            source_ids = {
                item.source_id for item in representations if item.source_id is not None
            }
            aliases = {
                item.id: item.alias
                for item in session.scalars(select(Source).where(Source.id.in_(source_ids))).all()
            }
            retained_locations = {
                (item.source_id, item.root_key, item.path_ref)
                for item in session.scalars(
                    select(Representation).where(
                        Representation.asset_id.not_in(target_ids),
                        Representation.deleted_at.is_(None),
                    )
                ).all()
            }
            file_targets: list[Path] = []
            skipped_shared: list[str] = []
            for representation in representations:
                location = (
                    representation.source_id,
                    representation.root_key,
                    representation.path_ref,
                )
                if location in retained_locations:
                    skipped_shared.append(str(representation.id))
                    continue
                if representation.source_id is None:
                    continue
                alias = aliases.get(representation.source_id)
                if alias is None:
                    continue
                target = _select_delete_target(
                    registry=registry,
                    source_alias=alias,
                    representation=representation,
                )
                if target is not None:
                    file_targets.append(target)
            for asset in papers:
                for root in (GENERATED_ROOT / "papers", EXTRACTION_ROOT):
                    target = (root / str(asset.id)).resolve()
                    if target.exists() and _contained(target, root):
                        file_targets.append(target)
            file_targets = _minimal_paths(file_targets)
            preview = {
                "assets": [
                    {"id": str(item.id), "type": item.asset_type, "title": item.title}
                    for item in sorted(assets, key=lambda value: (value.asset_type, value.title))
                ],
                "file_targets": [str(path) for path in file_targets],
                "shared_representations_preserved": skipped_shared,
            }
            if not execute:
                return {
                    "schema": "research-asset-hub.vln-uav-hard-delete/v1",
                    "status": "preview",
                    **preview,
                }
            session.execute(
                delete(Relation)
                .where(
                    or_(
                        Relation.subject_asset_id.in_(target_ids),
                        Relation.object_asset_id.in_(target_ids),
                    )
                )
                .execution_options(synchronize_session=False)
            )
            session.execute(
                delete(Asset)
                .where(Asset.id.in_(target_ids))
                .execution_options(synchronize_session=False)
            )
            session.commit()

        removed: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for path in file_targets:
            try:
                removed.append({"path": str(path), "kind": _remove_path(path)})
            except Exception as error:
                failures.append(
                    {
                        "path": str(path),
                        "error_type": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )
        return {
            "schema": "research-asset-hub.vln-uav-hard-delete/v1",
            "status": "passed" if not failures else "partial",
            "database": {
                "papers_deleted": EXPECTED_PAPERS,
                "repositories_deleted": EXPECTED_REPOSITORIES,
            },
            "files_removed": removed,
            "file_failures": failures,
            "shared_representations_preserved": skipped_shared,
            "recovery": "not recoverable from the active library",
        }
    finally:
        database.dispose()


def main() -> int:
    arguments = _arguments()
    if arguments.retry_file_failures:
        report = retry_file_failures(arguments.report)
    else:
        report = run_delete(policy_path=arguments.policy, execute=arguments.execute)
    atomic_write_json(arguments.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "database": report.get("database"),
                "asset_count": len(report.get("assets", [])),
                "file_target_count": len(report.get("file_targets", [])),
                "files_removed": len(report.get("files_removed", [])),
                "file_failures": len(report.get("file_failures", [])),
                "report": str(arguments.report),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] in {"preview", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
