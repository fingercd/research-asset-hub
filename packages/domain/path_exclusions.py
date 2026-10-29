"""Reversible diagnostic exclusions; never part of a source or record payload."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator, model_validator

RootMap = Mapping[tuple[str, str], Path]
POLICY_FILENAME = "operational-path-exclusions.yaml"


def _path_key(value: str | Path) -> str:
    normalized = str(value).replace("\\", "/").rstrip("/")
    return normalized.casefold() if os.name == "nt" else normalized


def _relative(value: str) -> str:
    value = value.replace("\\", "/")
    if (
        not value or any(character in value for character in "\0:*?[]")
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Exclusion paths must be exact, safe relative paths")
    return value


def _missing(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _present(path: Path, *, directory: bool) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)


def _contained(root: Path, path_ref: str) -> Path | None:
    try:
        path = (root / _relative(path_ref)).resolve()
        path.relative_to(root)
        return path
    except (OSError, ValueError):
        return None


class RootExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source_id: str
    root_key: str
    expected_root: str
    replacement_source_id: str
    replacement_root_key: str
    reason: str

    @field_validator("id", "source_id", "root_key", "replacement_source_id", "replacement_root_key")
    @classmethod
    def exact_key(cls, value: str) -> str:
        if not value or value != value.strip() or any(c in value for c in "\0/*?[]\\"):
            raise ValueError("Exclusion keys must be exact non-empty identifiers")
        return value

    @field_validator("root_key", "replacement_root_key")
    @classmethod
    def asset_root_only(cls, value: str) -> str:
        if value == "source":
            raise ValueError("Source roots and manifests cannot be excluded")
        return value

    @field_validator("expected_root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise ValueError("expected_root must be an absolute audited path")
        if any(c in value for c in "\0*?[]") or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("expected_root must not contain traversal or patterns")
        return value

    @field_validator("reason")
    @classmethod
    def reason_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("An exclusion requires a reason")
        return value


class PathExclusion(RootExclusion):
    source_record_id: str
    representation_type: Literal["annotation"]
    role: Literal["annotated"]
    path_ref: str
    replacement_path_ref: str

    @field_validator("source_record_id")
    @classmethod
    def exact_record(cls, value: str) -> str:
        return RootExclusion.exact_key(value)

    _safe_paths = field_validator("path_ref", "replacement_path_ref")(_relative)


class OperationalPathExclusions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    enabled: bool
    approved_at: str | None = None
    roots: tuple[RootExclusion, ...] = ()
    paths: tuple[PathExclusion, ...] = ()
    _sha256: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def unique_rules(self) -> OperationalPathExclusions:
        rules = [*self.roots, *self.paths]
        if len({rule.id for rule in rules}) != len(rules):
            raise ValueError("Duplicate exclusion rule ID")
        root_keys = {(rule.source_id, rule.root_key) for rule in self.roots}
        if len(root_keys) != len(self.roots):
            raise ValueError("Duplicate excluded source/root")
        path_keys = {
            (r.source_id, r.root_key, r.source_record_id, r.representation_type, r.role, r.path_ref)
            for r in self.paths
        }
        if len(path_keys) != len(self.paths):
            raise ValueError("Duplicate excluded representation selector")
        for rule in rules:
            if (rule.replacement_source_id, rule.replacement_root_key) in root_keys:
                raise ValueError("Replacement roots must not themselves be excluded")
        for rule in self.paths:
            if (
                rule.source_id == rule.replacement_source_id
                and rule.root_key == rule.replacement_root_key
                and rule.path_ref == rule.replacement_path_ref
            ):
                raise ValueError("An excluded path cannot replace itself")
        return self

    @classmethod
    def empty(cls) -> OperationalPathExclusions:
        return cls(schema_version=1, enabled=False)

    @classmethod
    def load(cls, path: Path | None) -> OperationalPathExclusions:
        if path is None or not path.is_file():
            return cls.empty()
        raw = path.read_bytes()
        result = cls.model_validate(yaml.safe_load(raw.decode("utf-8")))
        result._sha256 = hashlib.sha256(raw).hexdigest()
        return result

    @classmethod
    def for_sources_config(cls, path: Path) -> OperationalPathExclusions:
        return cls.load(path.parent / POLICY_FILENAME)

    def validate_roots(self, roots: RootMap) -> None:
        for rule in [*self.roots, *self.paths]:
            for key in (
                (rule.source_id, rule.root_key),
                (rule.replacement_source_id, rule.replacement_root_key),
            ):
                if key not in roots:
                    raise ValueError(f"Unregistered operational exclusion root: {key}")

    def active_root(self, source_id: str, root_key: str, roots: RootMap) -> RootExclusion | None:
        if not self.enabled:
            return None
        root = roots[(source_id, root_key)]
        for rule in self.roots:
            if (rule.source_id, rule.root_key) != (source_id, root_key):
                continue
            replacement = roots[(rule.replacement_source_id, rule.replacement_root_key)]
            if (
                _path_key(root) == _path_key(rule.expected_root)
                and _missing(root)
                and _present(replacement, directory=True)
            ):
                return rule
        return None

    def active_reference(
        self, source_id: str, root_key: str, path_ref: str, roots: RootMap, *,
        source_record_id: str | None, representation_type: str, role: str,
    ) -> RootExclusion | PathExclusion | None:
        if not self.enabled:
            return None
        root = roots[(source_id, root_key)]
        target = _contained(root, path_ref)
        if target is None or not _missing(target):
            return None
        root_rule = self.active_root(source_id, root_key, roots)
        if root_rule is not None:
            return root_rule
        for rule in self.paths:
            if (
                rule.source_id != source_id or rule.root_key != root_key
                or rule.source_record_id != source_record_id
                or rule.representation_type != representation_type or rule.role != role
                or rule.path_ref != path_ref
                or _path_key(root) != _path_key(rule.expected_root)
            ):
                continue
            replacement_root = roots[(rule.replacement_source_id, rule.replacement_root_key)]
            replacement = _contained(replacement_root, rule.replacement_path_ref)
            if replacement is not None and _present(replacement, directory=False):
                return rule
        return None

    def summary(self, applied: set[str] | None = None) -> dict[str, object]:
        applied = applied or set()
        all_ids = {rule.id for rule in [*self.roots, *self.paths]}
        return {
            "enabled": self.enabled, "sha256": self._sha256,
            "root_rules": len(self.roots), "path_rules": len(self.paths),
            "applied_rule_ids": sorted(applied),
            "unused_or_inactive_rule_ids": sorted(all_ids - applied),
        }
