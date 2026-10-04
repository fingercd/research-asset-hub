from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock

import yaml

from packages.domain.path_exclusions import POLICY_FILENAME, OperationalPathExclusions
from packages.managed_sources.service import (
    data_directory,
    load_managed_sources,
    managed_source_origin,
    relocated_managed_path,
    source_workspace_root,
)

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class UnsafeAssetPath(ValueError):
    """Raised when a registered path would escape its configured source root."""


class SourceConfigurationError(ValueError):
    """The current source configuration could not replace the last valid snapshot."""


@dataclass(frozen=True, slots=True)
class SourceRoot:
    source_id: str
    root_key: str
    path: Path


@dataclass(frozen=True, slots=True)
class _RegistrySnapshot:
    roots: dict[tuple[str, str], Path]
    workspace_root: Path | None
    exclusions: OperationalPathExclusions
    signature: tuple[object, ...] | None = None
    config_sha256: str | None = None


class SourceRegistry:
    """Resolve only source/root/path triples explicitly registered in sources.yaml."""

    def __init__(
        self,
        roots: dict[tuple[str, str], Path],
        *,
        workspace_root: Path | None = None,
        operational_exclusions: OperationalPathExclusions | None = None,
    ) -> None:
        exclusions = operational_exclusions or OperationalPathExclusions.empty()
        if exclusions.enabled:
            exclusions.validate_roots(roots)
        self._snapshot = _RegistrySnapshot(dict(roots), workspace_root, exclusions)
        self._config_path: Path | None = None
        self._data_dir: Path | None = None
        self._reload_lock = Lock()

    @property
    def operational_exclusions(self) -> OperationalPathExclusions:
        self.refresh_if_changed()
        return self._snapshot.exclusions

    @property
    def config_path(self) -> Path | None:
        """Actual runtime configuration, which may be an installed copy."""
        return self._config_path

    @staticmethod
    def _signature(config_path: Path, data_dir: Path | None = None) -> tuple[object, ...]:
        def file_signature(path: Path) -> tuple[int, int, int] | None:
            try:
                info = path.stat()
            except FileNotFoundError:
                return None
            return info.st_mtime_ns, info.st_ctime_ns, info.st_size

        return (
            file_signature(config_path),
            file_signature(config_path.parent / POLICY_FILENAME),
            file_signature(config_path.parent / "workspace-root.txt"),
            os.environ.get("HUB_HOST_WORKSPACE_ROOT", "").strip(),
            file_signature(data_dir / "config" / "managed-sources.yaml") if data_dir else None,
        )

    @classmethod
    def load(cls, config_path: Path, *, data_dir: Path | None = None) -> SourceRegistry:
        registry = cls({})
        registry._config_path = config_path.expanduser().resolve()
        registry._data_dir = Path(data_dir).expanduser().resolve() if data_dir else data_directory()
        registry.refresh_if_changed()
        return registry

    def _read_snapshot(self, config_path: Path, signature: tuple[object, ...]) -> _RegistrySnapshot:
        data_dir = self._data_dir
        raw = config_path.read_bytes()
        document = yaml.load(raw.decode("utf-8"), Loader=_YAML_LOADER)
        if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
            raise ValueError("Source configuration must contain a sources list.")
        workspace_root = source_workspace_root(config_path, document)
        roots: dict[tuple[str, str], Path] = {}
        managed = load_managed_sources(data_dir) if data_dir else []
        managed_ids = {source["id"] for source in managed}
        sources = [
            *document.get("sources", []),
            *managed,
        ]
        for raw_source in sources:
            if not isinstance(raw_source, dict):
                raise ValueError("Each source must be a mapping.")
            source_id = str(raw_source.get("id", "")).strip()
            if not source_id or (source_id, "source") in roots:
                raise ValueError("Source identifiers must be non-empty and unique.")
            if raw_source.get("read_only", True) is not True:
                raise ValueError("Registered sources must be read-only.")
            if not isinstance(raw_source.get("root"), str) or not raw_source["root"]:
                raise ValueError("Each source must declare a root path.")
            source_base = (workspace_root / str(raw_source.get("root", "."))).resolve()
            origin = None
            if source_id in managed_ids and data_dir is not None:
                origin = managed_source_origin(raw_source)
                source_base = relocated_managed_path(source_base, origin, data_dir).resolve()
            roots[(source_id, "source")] = source_base
            asset_roots = raw_source.get("asset_roots", {})
            if not isinstance(asset_roots, dict):
                raise ValueError("Source asset_roots must be a mapping.")
            for root_key, relative in asset_roots.items():
                if not isinstance(root_key, str) or not root_key or root_key == "source":
                    raise ValueError("Asset root keys must be non-empty and cannot replace source.")
                if not isinstance(relative, str) or not relative or "\0" in relative:
                    raise ValueError("Asset roots must declare valid paths.")
                resolved = source_base / relative
                if origin is not None and data_dir is not None:
                    resolved = relocated_managed_path(resolved, origin, data_dir)
                roots[(source_id, str(root_key))] = resolved.resolve()
        exclusions = OperationalPathExclusions.for_sources_config(config_path)
        if exclusions.enabled:
            exclusions.validate_roots(roots)
        return _RegistrySnapshot(
            roots,
            workspace_root,
            exclusions,
            signature,
            hashlib.sha256(
                raw
                + (
                    (data_dir / "config" / "managed-sources.yaml").read_bytes()
                    if data_dir and (data_dir / "config" / "managed-sources.yaml").exists()
                    else b""
                )
            ).hexdigest(),
        )

    def refresh_if_changed(self) -> bool:
        """Replace one complete snapshot in place; retain it and fail clearly on errors.

        Ordinary resolution only stats the two small configuration files. Parsing,
        validation and locking happen when either configuration has changed.
        """
        if self._config_path is None:
            return False
        try:
            signature = self._signature(self._config_path, self._data_dir)
            if signature == self._snapshot.signature:
                return False
            with self._reload_lock:
                signature = self._signature(self._config_path, self._data_dir)
                if signature == self._snapshot.signature:
                    return False
                candidate = self._read_snapshot(self._config_path, signature)
                if self._signature(self._config_path, self._data_dir) != signature:
                    raise ValueError(
                        "Source configuration changed during reload; retry the request."
                    )
                self._snapshot = candidate
            return True
        except (OSError, ValueError, yaml.YAMLError) as error:
            # Do not expose host paths, parser excerpts or source configuration.
            raise SourceConfigurationError(
                "Source configuration reload failed; the previous complete registry is retained."
            ) from error

    def configuration_status(self) -> dict[str, object]:
        """A path-free fingerprint allows clients to compare runtime configurations."""
        self.refresh_if_changed()
        snapshot = self._snapshot
        return {
            "hot_refresh_enabled": self._config_path is not None,
            "configuration_sha256": snapshot.config_sha256,
            "configuration_locator_sha256": (
                hashlib.sha256(str(self._config_path).encode("utf-8")).hexdigest()
                if self._config_path
                else None
            ),
            "configuration_filename": self._config_path.name if self._config_path else None,
            "operational_exclusions_sha256": snapshot.exclusions.summary()["sha256"],
            "resolved_roots_sha256": hashlib.sha256(
                "\n".join(
                    f"{source_id}\0{root_key}\0{path}"
                    for (source_id, root_key), path in sorted(snapshot.roots.items())
                ).encode("utf-8")
            ).hexdigest(),
            "loaded_configuration_current": True if self._config_path else None,
            "repository_copy_consistency": "not_verified",
        }

    def root_for(self, source_id: str, root_key: str) -> Path:
        self.refresh_if_changed()
        try:
            return self._snapshot.roots[(source_id, root_key)]
        except KeyError as error:
            raise UnsafeAssetPath(
                f"Unknown registered source root {source_id!r}/{root_key!r}."
            ) from error

    def resolve(self, source_id: str, root_key: str, path_ref: str) -> Path:
        root = self.root_for(source_id, root_key)
        normalized = path_ref.replace("\\", "/")
        # Platform-owned directory representations register the exact selected
        # directory as their root; no parent or generic-manifest expansion occurs.
        if normalized == "." and root_key == "repository":
            return root
        candidate_ref = PurePosixPath(normalized)
        if (
            not normalized
            or "\0" in normalized
            or ":" in normalized
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
            or candidate_ref.is_absolute()
        ):
            raise UnsafeAssetPath("Registered path is absolute or contains traversal.")
        candidate = root.joinpath(*candidate_ref.parts).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise UnsafeAssetPath("Registered path escapes its source root.") from error
        return candidate

    def describe(self) -> list[SourceRoot]:
        self.refresh_if_changed()
        return [
            SourceRoot(source_id=source_id, root_key=root_key, path=path)
            for (source_id, root_key), path in sorted(self._snapshot.roots.items())
        ]

    def root_status(self) -> dict[str, object]:
        """Describe registration only; historical roots do not measure live availability."""
        self.refresh_if_changed()
        snapshot = self._snapshot
        return {
            "workspace_root": (
                str(snapshot.workspace_root) if snapshot.workspace_root is not None else None
            ),
            "configured": len(snapshot.roots),
            "verification": "registration_only",
            "source_files_verified": False,
            "operational_exclusions": snapshot.exclusions.summary(),
        }
