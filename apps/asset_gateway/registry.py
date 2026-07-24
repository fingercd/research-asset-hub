from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml


class UnsafeAssetPath(ValueError):
    """Raised when a registered path would escape its configured source root."""


@dataclass(frozen=True, slots=True)
class SourceRoot:
    source_id: str
    root_key: str
    path: Path


class SourceRegistry:
    """Resolve only source/root/path triples explicitly registered in sources.yaml."""

    def __init__(self, roots: dict[tuple[str, str], Path]) -> None:
        self._roots = roots

    @classmethod
    def load(cls, config_path: Path) -> SourceRegistry:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config_dir = config_path.parent
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
        return cls(roots)

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
