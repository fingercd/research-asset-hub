"""Download registered repository URLs into their declared local asset roots.

The script is read-only with respect to the database: it never changes
representations or frozen manifests. ``--dry-run`` (the default) only prints
what would be cloned. Use ``--apply`` to run ``git clone``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings, get_settings
from apps.api.database import create_database_engine
from apps.api.editor import CODE_REPRESENTATION_TYPES, repository_components, resolve_editor_path
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.models import Representation, Source


@dataclass(slots=True)
class LocalRepositoryReport:
    dry_run: bool
    scanned: int = 0
    skipped: int = 0
    skipped_non_code: int = 0
    downloaded: int = 0
    failed: int = 0
    no_source: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clone_repository(
    url: str,
    destination: Path,
    *,
    shallow: bool,
) -> tuple[bool, str]:
    destination_exists = destination.exists()
    try:
        arguments = ["git", "clone"]
        if shallow:
            arguments.extend(["--depth", "1"])
        arguments.extend([url, str(destination)])
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0:
            return True, "cloned"
        if destination_exists:
            return False, completed.stderr.strip() or "git clone failed"
        shutil.rmtree(destination, ignore_errors=True)
        if shallow:
            retry = subprocess.run(
                ["git", "clone", url, str(destination)],
                capture_output=True,
                text=True,
                timeout=1800,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if retry.returncode == 0:
                return True, "cloned after full-clone retry"
            shutil.rmtree(destination, ignore_errors=True)
            return False, retry.stderr.strip() or "git clone failed"
        return False, completed.stderr.strip() or "git clone failed"
    except (OSError, subprocess.SubprocessError) as error:
        if not destination_exists:
            shutil.rmtree(destination, ignore_errors=True)
        return False, f"{type(error).__name__}: {error}"


_CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})


def _clone_url(url: str) -> tuple[str, str | None]:
    """Return a canonical clone URL and a reason for non-code resources."""

    try:
        parsed = urlparse(url)
    except ValueError:
        return url, "源码地址格式无效"
    host = (parsed.hostname or "").casefold()
    if host == "huggingface.co":
        return url, "Hugging Face 模型/数据集仓库不是论文核心源码，不下载权重或数据集"
    if host not in _CODE_HOSTS:
        return url, "不是已确认的公开 Git 代码仓库"
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return url, "无法从地址推导 Git 仓库"
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return f"https://{host}/{owner}/{repo}.git", None


def _has_visible_entries(path: Path) -> bool:
    try:
        return any(not child.name.startswith(".") for child in path.iterdir())
    except OSError:
        return False


def _run(
    session: Session,
    registry: SourceRegistry,
    *,
    apply: bool,
    source_alias: str | None,
    limit: int | None,
    shallow: bool,
) -> LocalRepositoryReport:
    report = LocalRepositoryReport(dry_run=not apply)
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

    for representation, source in rows:
        report.scanned += 1
        representation_id = str(representation.id)
        source_id = str(source.alias)
        root_key = representation.root_key or "repository"
        resolution = resolve_editor_path(
            registry,
            representation,
            source_alias=source_id,
        )
        if resolution.available:
            report.skipped += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "skipped",
                    "path": resolution.path,
                }
            )
            continue
        if not representation.external_url:
            report.no_source += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "no-source",
                    "reason": "没有可下载的源码地址",
                }
            )
            continue
        clone_url, skip_reason = _clone_url(representation.external_url)
        if skip_reason is not None:
            report.skipped_non_code += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "skipped-non-code",
                    "url": representation.external_url,
                    "reason": skip_reason,
                }
            )
            continue
        components = repository_components(clone_url)
        if components is None:
            report.no_source += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "no-source",
                    "reason": "无法从 URL 推导仓库名",
                }
            )
            continue
        try:
            root = registry.root_for(source_id, root_key)
        except UnsafeAssetPath:
            report.failed += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "failed",
                    "reason": f"未登记的资产根目录 {root_key}",
                }
            )
            continue
        if not root.is_dir():
            report.failed += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "failed",
                    "reason": f"资产根目录不存在: {root}",
                }
            )
            continue
        owner, repo = components
        destination = root / f"{owner}-{repo}"
        if destination.is_dir() and _has_visible_entries(destination):
            report.skipped += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "skipped",
                    "path": destination.as_posix(),
                }
            )
            continue
        if destination.exists():
            report.failed += 1
            report.actions.append(
                {
                    "source": source_id,
                    "representation_id": representation_id,
                    "outcome": "failed",
                    "url": representation.external_url,
                    "path": destination.as_posix(),
                    "reason": "目标目录已存在但不是可用代码仓库",
                }
            )
            continue
        action: dict[str, Any] = {
            "source": source_id,
            "representation_id": representation_id,
            "outcome": "would-download" if not apply else "downloading",
            "url": clone_url,
            "path": destination.as_posix(),
        }
        if not apply:
            report.actions.append(action)
            continue
        success, detail = _clone_repository(
            clone_url,
            destination,
            shallow=shallow,
        )
        if success:
            report.downloaded += 1
            action["outcome"] = "downloaded"
            action["detail"] = detail
        else:
            report.failed += 1
            action["outcome"] = "failed"
            action["detail"] = detail
        report.actions.append(action)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--database-url")
    parser.add_argument("--full-clone", action="store_true", help="clone full history")
    parser.add_argument("--report", type=Path, help="also write the JSON report to this path")
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
            report = _run(
                session,
                registry,
                apply=arguments.apply,
                source_alias=arguments.source,
                limit=arguments.limit,
                shallow=not arguments.full_clone,
            )
    finally:
        engine.dispose()
    serialized = json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
