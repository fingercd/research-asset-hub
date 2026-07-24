from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldConflict:
    field: str
    base_value: Any
    hub_value: Any
    external_value: Any
    reason: str = "concurrent_change"


@dataclass(frozen=True, slots=True)
class SyncPreview:
    merged: dict[str, Any]
    conflicts: tuple[FieldConflict, ...]
    hub_changed: tuple[str, ...]
    external_changed: tuple[str, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts


_MISSING = object()


def three_way_merge(
    base: dict[str, Any],
    hub: dict[str, Any],
    external: dict[str, Any],
) -> SyncPreview:
    """Merge one sync target without silently discarding concurrent edits."""

    merged: dict[str, Any] = {}
    conflicts: list[FieldConflict] = []
    hub_changed: list[str] = []
    external_changed: list[str] = []

    for field in sorted(set(base) | set(hub) | set(external)):
        base_value = base.get(field, _MISSING)
        hub_value = hub.get(field, _MISSING)
        external_value = external.get(field, _MISSING)
        hub_is_changed = hub_value != base_value
        external_is_changed = external_value != base_value
        if hub_is_changed:
            hub_changed.append(field)
        if external_is_changed:
            external_changed.append(field)

        selected = _MISSING
        if hub_value == external_value:
            selected = hub_value
        elif not hub_is_changed:
            selected = external_value
        elif not external_is_changed:
            selected = hub_value
        else:
            conflicts.append(
                FieldConflict(
                    field=field,
                    base_value=None if base_value is _MISSING else base_value,
                    hub_value=None if hub_value is _MISSING else hub_value,
                    external_value=None if external_value is _MISSING else external_value,
                    reason=(
                        "delete_vs_edit"
                        if _MISSING in (hub_value, external_value)
                        else "concurrent_change"
                    ),
                )
            )
            selected = hub_value

        if selected is not _MISSING:
            merged[field] = selected

    return SyncPreview(
        merged=merged,
        conflicts=tuple(conflicts),
        hub_changed=tuple(hub_changed),
        external_changed=tuple(external_changed),
    )
