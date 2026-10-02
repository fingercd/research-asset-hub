from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from packages.domain.path_exclusions import OperationalPathExclusions

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class UnsafeAssetPath(ValueError):
    """Raised when a registered path would escape its configured source root."""


@dataclass(frozen=True, slots=True)
class SourceRoot:
    source_id: str
    root_key: str
    path: Path


class SourceRegistry:
    """Resolve only source/root/path triples explicitly registered in sources.yaml."""

    def __init__(
        self,
        roots: dict[tuple[str, str], Path],
        *,
        workspace_root: Path | None = None,
        operational_exclusions: OperationalPathExclusions | None = None,
    ) -> None:
        self._roots = roots
        self._workspace_root = workspace_root
        self.operational_exclusions = operational_exclusions or OperationalPathExclusions.empty()
        if self.operational_exclusions.enabled:
            self.operational_exclusions.validate_roots(roots)

    @classmethod
    def load(cls, config_path: Path) -> SourceRegistry:
        document = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_YAML_LOADER) or {}
        config_dir = config_path.parent
        host_workspace_root = os.environ.get("HUB_HOST_WORKSPACE_ROOT", "").strip()
        if host_workspace_root:
            workspace_root = Path(host_workspace_root).expanduser().resolve()
        else:
            workspace_root = (config_dir / str(document.get("workspace_root", "."))).resolve()
        roots: dict[tuple[str, str], Path] = {}
        for raw_source in document.get("sources", []):
            if not isinstance(raw_source, dict):
                continue
            source_id = str(raw_source.get("id", "")).strip()
            source_base = (workspace_root / str(raw_source.get("root", "."))).resolve()
            roots[(source_id, "source")] = source_base
            asset_roots = raw_source.get("asset_roots", {})
            if not isinstance(asset_roots, dict):
                continue
            for root_key, relative in asset_roots.items():
                roots[(source_id, str(root_key))] = (source_base / str(relative)).resolve()
        return cls(
            roots,
            workspace_root=workspace_root,
            operational_exclusions=OperationalPathExclusions.for_sources_config(config_path),
        )

    def root_for(self, source_id: str, root_key: str) -> Path:
        try:
            return self._roots[(source_id, root_key)]
        except KeyError as error:
            raise UnsafeAssetPath(
                f"Unknown registered source root {source_id!r}/{root_key!r}."
            ) from error

    def resolve(self, source_id: str, root_key: str, path_ref: str) -> Path:
        root = self.root_for(source_id, root_key)
        candidate_ref = PurePosixPath(path_ref.replace("\\", "/"))
        if candidate_ref.is_absolute() or ".." in candidate_ref.parts:
            raise UnsafeAssetPath("Registered path is absolute or contains traversal.")
        candidate = root.joinpath(*candidate_ref.parts).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise UnsafeAssetPath("Registered path escapes its source root.") from error
        return candidate

    def describe(self) -> list[SourceRoot]:
        return [
            SourceRoot(source_id=source_id, root_key=root_key, path=path)
            for (source_id, root_key), path in sorted(self._roots.items())
        ]

    def root_status(self) -> dict[str, object]:
        """Return a small diagnostic summary without probing any source contents."""
        configured = self.describe()
        missing: list[SourceRoot] = []
        invalid: list[dict[str, str]] = []
        for item in configured:
            reason = None
            try:
                if not stat.S_ISDIR(item.path.stat().st_mode):
                    reason = "not_directory"
            except FileNotFoundError:
                missing.append(item)
            except OSError as error:
                reason = type(error).__name__
            if reason:
                invalid.append(
                    {
                        "source_id": item.source_id,
                        "root_key": item.root_key,
                        "path": str(item.path),
                        "reason": reason,
                    }
                )
        ignored = []
        applied: set[str] = set()
        for item in missing:
            rule = self.operational_exclusions.active_root(
                item.source_id, item.root_key, self._roots
            )
            if rule is not None:
                applied.add(rule.id)
                ignored.append(
                    {
                        "source_id": item.source_id,
                        "root_key": item.root_key,
                        "path": str(item.path),
                        "rule_id": rule.id,
                        "reason": rule.reason,
                    }
                )
        return {
            "workspace_root": (
                str(self._workspace_root) if self._workspace_root is not None else None
            ),
            "configured": len(configured),
            "existing": len(configured) - len(missing) - len(invalid),
            "missing": len(missing),
            "ignored_missing": len(ignored),
            "actionable_missing": len(missing) - len(ignored),
            "invalid_roots": invalid,
            "ignored_missing_roots": ignored,
            "operational_exclusions": self.operational_exclusions.summary(applied),
            "missing_roots": [
                {
                    "source_id": item.source_id,
                    "root_key": item.root_key,
                    "path": str(item.path),
                }
                for item in missing
            ],
        }
