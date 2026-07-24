from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from packages.domain.schemas import NormalizedRecord

_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


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
