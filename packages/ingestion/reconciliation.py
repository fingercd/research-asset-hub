from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from packages.domain.schemas import NormalizedRecord

_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
DEFAULT_WEAK_MERGE_APPROVALS = (
    Path(__file__).resolve().parents[2] / "config" / "identity-merge-approvals.yaml"
)


class UnapprovedWeakMergeError(ValueError):
    """Raised before database writes when a weak identity group lacks approval."""

    def __init__(self, groups: list[dict[str, Any]]) -> None:
        self.groups = groups
        rendered = json.dumps(groups, ensure_ascii=False, sort_keys=True)
        super().__init__(
            "Unapproved title/year identity merge candidates must be reviewed "
            f"and allowlisted before formal import: {rendered}"
        )


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _PUNCTUATION.sub(" ", normalized)
    return _SPACE.sub(" ", normalized).strip()


def canonical_identifier(scheme: str, value: str) -> str:
    normalized_scheme = scheme.strip().casefold().replace("_", "-")
    normalized_value = unicodedata.normalize("NFKC", value).strip()
    if normalized_scheme == "doi":
        normalized_value = normalized_value.casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if normalized_value.startswith(prefix):
                normalized_value = normalized_value[len(prefix) :]
                break
    elif normalized_scheme in {"arxiv", "arxiv-id"}:
        normalized_scheme = "arxiv"
        normalized_value = normalized_value.casefold()
        for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
            if normalized_value.startswith(prefix):
                normalized_value = normalized_value[len(prefix) :]
                break
        normalized_value = re.sub(r"v\d+$", "", normalized_value)
    return f"{normalized_scheme}:{normalized_value}"


def record_identity_keys(record: NormalizedRecord) -> dict[str, list[str]]:
    strong: list[str] = []
    for identifier in record.asset.external_identifiers:
        scheme = str(identifier.scheme)
        if scheme.casefold() in {"doi", "arxiv", "arxiv-id"}:
            strong.append(canonical_identifier(scheme, identifier.value))

    weak: list[str] = []
    title = normalize_title(record.asset.title)
    if title:
        weak.append(f"title-year:{title}:{record.asset.year or 'unknown'}")
    return {
        "strong": sorted(set(strong)),
        "weak": sorted(set(weak)),
    }


def stable_record_payload(record: NormalizedRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json", by_alias=False)
    payload.pop("normalized_at", None)
    asset = payload.get("asset")
    if isinstance(asset, dict):
        asset.pop("asset_id", None)
    return payload


def record_fingerprint(record: NormalizedRecord) -> str:
    encoded = json.dumps(
        stable_record_payload(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordLocator:
    source_id: str
    source_record_id: str

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.source_record_id}"


def _group_shared_keys(key_to_records: dict[str, list[RecordLocator]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for identity_key, locators in sorted(key_to_records.items()):
        unique = {locator.key: locator for locator in locators}
        source_ids = {locator.source_id for locator in unique.values()}
        if len(unique) < 2 or len(source_ids) < 2:
            continue
        groups.append(
            {
                "identity_key": identity_key,
                "records": [
                    {
                        "source_id": locator.source_id,
                        "source_record_id": locator.source_record_id,
                    }
                    for locator in sorted(
                        unique.values(),
                        key=lambda item: (item.source_id, item.source_record_id),
                    )
                ],
            }
        )
    return groups


def load_approved_weak_merges(
    path: Path = DEFAULT_WEAK_MERGE_APPROVALS,
) -> dict[str, frozenset[str]]:
    """Load exact title/year merge approvals from the audited project config."""

    if not path.is_file():
        raise ValueError(f"Weak merge approval file does not exist: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise ValueError("Weak merge approval file must use schema_version: 1")
    entries = document.get("approved_weak_merge_groups")
    if not isinstance(entries, list):
        raise ValueError("approved_weak_merge_groups must be a list")

    approvals: dict[str, frozenset[str]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"approved_weak_merge_groups[{index}] must be an object")
        identity_key = str(entry.get("identity_key", "")).strip()
        records = entry.get("records")
        if not identity_key.startswith("title-year:"):
            raise ValueError(
                f"approved_weak_merge_groups[{index}] must use a title-year identity key"
            )
        if not isinstance(records, list):
            raise ValueError(f"approved_weak_merge_groups[{index}].records must be a list")
        record_keys: set[str] = set()
        for record_index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ValueError(
                    f"approved_weak_merge_groups[{index}].records[{record_index}] "
                    "must be an object"
                )
            source_id = str(record.get("source_id", "")).strip()
            source_record_id = str(record.get("source_record_id", "")).strip()
            if not source_id or not source_record_id:
                raise ValueError(
                    f"approved_weak_merge_groups[{index}].records[{record_index}] "
                    "requires source_id and source_record_id"
                )
            record_keys.add(f"{source_id}:{source_record_id}")
        if len(record_keys) < 2:
            raise ValueError(
                f"approved_weak_merge_groups[{index}] must contain at least two records"
            )
        if identity_key in approvals:
            raise ValueError(f"Duplicate weak merge approval: {identity_key}")
        approvals[identity_key] = frozenset(record_keys)
    return approvals


def apply_weak_merge_approvals(
    reconciliation: dict[str, Any],
    *,
    approvals: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    """Classify review candidates against an exact, auditable allowlist."""

    configured = dict(
        load_approved_weak_merges() if approvals is None else approvals
    )
    approved: list[dict[str, Any]] = []
    unapproved: list[dict[str, Any]] = []
    observed_keys: set[str] = set()
    for group in reconciliation["needs_review_groups"]:
        identity_key = str(group["identity_key"])
        observed_keys.add(identity_key)
        record_keys = frozenset(
            f"{record['source_id']}:{record['source_record_id']}"
            for record in group["records"]
        )
        expected = configured.get(identity_key)
        if expected == record_keys:
            approved.append(group)
            continue
        unapproved.append(
            {
                **group,
                "approval_status": (
                    "record_set_changed" if expected is not None else "not_allowlisted"
                ),
            }
        )

    return {
        **reconciliation,
        "approved_review_groups": approved,
        "unapproved_review_groups": unapproved,
        "unused_approval_keys": sorted(set(configured) - observed_keys),
    }


def reconcile_records(records: Iterable[NormalizedRecord]) -> dict[str, Any]:
    strong_index: dict[str, list[RecordLocator]] = defaultdict(list)
    weak_index: dict[str, list[RecordLocator]] = defaultdict(list)
    source_keys: set[str] = set()
    duplicate_source_keys: list[str] = []
    fingerprints: dict[str, str] = {}

    record_count = 0
    for record in records:
        record_count += 1
        locator = RecordLocator(record.source_id, record.source_record_id)
        if locator.key in source_keys:
            duplicate_source_keys.append(locator.key)
        source_keys.add(locator.key)
        fingerprints[locator.key] = record_fingerprint(record)
        keys = record_identity_keys(record)
        for key in keys["strong"]:
            strong_index[key].append(locator)
        for key in keys["weak"]:
            weak_index[key].append(locator)

    auto_merge_groups = _group_shared_keys(strong_index)
    weak_groups = _group_shared_keys(weak_index)
    auto_merge_record_keys = {
        f"{record['source_id']}:{record['source_record_id']}"
        for group in auto_merge_groups
        for record in group["records"]
    }

    review_groups: list[dict[str, Any]] = []
    for group in weak_groups:
        record_keys = {
            f"{record['source_id']}:{record['source_record_id']}" for record in group["records"]
        }
        if record_keys.issubset(auto_merge_record_keys):
            continue
        review_groups.append(group)

    return {
        "record_count": record_count,
        "unique_source_record_keys": len(source_keys),
        "duplicate_source_record_keys": sorted(set(duplicate_source_keys)),
        "auto_merge_groups": auto_merge_groups,
        "needs_review_groups": review_groups,
        "record_fingerprints": dict(sorted(fingerprints.items())),
    }
