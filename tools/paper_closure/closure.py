#!/usr/bin/env python3
"""Build a read-only, paper-level closure report from a paper census and Hub evidence.

The tool deliberately separates identity evidence into three classes:

* strong: DOI, arXiv, or a declared PDF SHA-256;
* declared: an exact registered local representation path or Hub provenance;
* weak: normalized/fuzzy title similarity.

Only strong and declared evidence can select a canonical Hub candidate.  Weak
title evidence is always emitted for review and never merged automatically.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from packages.adapters import DEFAULT_REGISTRY
from packages.domain.schemas import SourceConfig

SCHEMA_VERSION = "paper-closure/v1"
CENSUS_SCHEMA_VERSION = "paper-census/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARXIV_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
IDENTITY_RELATIONS = frozenset({"same_as", "version_of"})
CODE_TYPES = frozenset({"repository", "code"})
GUIDE_CODE_STATUSES = frozenset({"none_found", "announced_pending", "third_party_only"})
EXPLICIT_CODE_STATUS_MAP = {
    "none_found": "explicit_no_code",
    "none": "explicit_no_code",
    "no_code": "explicit_no_code",
    "no_code_found": "explicit_no_code",
    "no_public_code": "explicit_no_code",
    "no_public_code_found": "explicit_no_code",
    "no_confirmed_code_found": "explicit_no_code",
    "no_official_code": "explicit_no_code",
    "not_applicable": "explicit_no_code",
    "announced_pending": "announced_pending",
    "third_party_only": "third_party_only",
    "reference_or_community_only": "third_party_only",
}
HTML_TYPES = frozenset({"html"})
PDF_TYPES = frozenset({"pdf"})

# A full-root census also sees PDFs shipped inside cloned repositories (posters,
# figures, examples, benchmark outputs) and test fixtures.  They are useful
# evidence, but are not paper records.  Keep this policy deliberately small and
# path-based so every excluded row remains visible and auditable in the report.
_REPOSITORY_PATH_MARKERS = frozenset(
    {"code", "codes", "repo", "repos", "repository", "repositories", "upstream"}
)
_ATTACHMENT_PATH_MARKERS = frozenset(
    {
        "appendix",
        "asset",
        "assets",
        "example",
        "examples",
        "figure",
        "figures",
        "image",
        "images",
        "pics",
        "poster",
        "presentation",
        "slides",
        "supplement",
        "supplementary",
        "teaser",
    }
)
_INVALID_PATH_MARKERS = frozenset(
    {"fixture", "fixtures", "pytest", "test", "tests", "tmp", "temp", "temporary"}
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return "" if raw is None else str(raw)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _get_in(value: Any, dotted: str | None) -> Any:
    if not dotted:
        return None
    current = value
    for component in dotted.split("."):
        if isinstance(current, Mapping):
            current = current.get(component)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            try:
                current = current[int(component)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _normalize_sha(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    normalized = text.casefold()
    return normalized if SHA256_RE.fullmatch(normalized) else None


def _normalize_arxiv(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = ARXIV_RE.search(text)
    return match.group(1).casefold() if match else None


def _normalize_doi(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;)]}").casefold()


def _identifier(scheme: Any, value: Any) -> str | None:
    scheme_text = _value(scheme).casefold().strip()
    if scheme_text == "arxiv":
        normalized = _normalize_arxiv(value)
    elif scheme_text == "doi":
        normalized = _normalize_doi(value)
    else:
        return None
    return f"{scheme_text}:{normalized}" if normalized else None


def _identifiers_from_text(*values: Any) -> set[str]:
    result: set[str] = set()
    for value in values:
        text = _text(value)
        if not text:
            continue
        arxiv = _normalize_arxiv(text)
        doi = _normalize_doi(text)
        if arxiv:
            result.add(f"arxiv:{arxiv}")
        if doi:
            result.add(f"doi:{doi}")
    return result


def _normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value) or "").casefold()
    # Keep letters and digits from all scripts; formatting and punctuation are noise.
    return "".join(character for character in text if character.isalnum())


def _title_from_filename(path: str) -> str:
    stem = Path(path).stem
    stem = ARXIV_RE.sub(" ", stem)
    stem = YEAR_RE.sub(" ", stem)
    stem = re.sub(r"(?:read|unread|已读|未读|paper|arxiv|pdf)", " ", stem, flags=re.I)
    return re.sub(r"[_\-–—]+", " ", stem).strip()


def _infer_year(*values: Any) -> int | None:
    for value in values:
        text = _text(value)
        if not text:
            continue
        match = YEAR_RE.search(text)
        if match:
            return int(match.group(1))
    return None


def _norm_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


def _path_is_within(value: str | Path, root: str | Path | None) -> bool | None:
    if root is None:
        return None
    try:
        path = _norm_path(value)
        parent = _norm_path(root)
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _path_segments(value: str) -> set[str]:
    """Return normalized path components without treating file stems as evidence."""

    return {
        component.casefold()
        for component in re.split(r"[\\\\/]", value)
        if component and component not in {".", ".."}
    }


def _classify_census_pdf(
    *,
    path_for_classification: str,
    in_acquisition_batch: bool,
    exact_declared_pdf_path: bool,
) -> tuple[str, str]:
    """Classify a scanned PDF before applying formal-paper closure gates.

    The explicit Desktop acquisition batch and a path formally declared by the
    Hub always win.  Everything else is only classified as non-paper when its
    path itself says it lives in a repository/document/fixture tree; ambiguous
    paths remain ``formal_paper`` and therefore require ordinary closure.
    """

    if in_acquisition_batch:
        return "formal_paper", "explicit_acquisition_batch"
    if exact_declared_pdf_path:
        return "formal_paper", "exact_hub_declared_pdf_path"
    segments = _path_segments(path_for_classification)
    repository_markers = sorted(segments & _REPOSITORY_PATH_MARKERS)
    if repository_markers:
        return (
            "repository_document",
            "undeclared_pdf_under_repository_tree:" + ",".join(repository_markers),
        )
    invalid_markers = sorted(segments & _INVALID_PATH_MARKERS)
    # Pytest's temporary directories use names such as ``pytest-foo`` and
    # ``test_bar`` rather than a literal ``pytest``/``test`` component.  They
    # are generated fixtures, not user papers; recognize these prefixes only
    # when the path is below a work/test tree to avoid misclassifying a paper
    # whose title happens to contain the word "test".
    if "work" in segments or "test" in segments or "tests" in segments:
        invalid_markers.extend(
            component
            for component in segments
            if component.startswith(("pytest-", "pytest_", "test-", "test_", "tmp-", "tmp_"))
        )
    invalid_markers = sorted(set(invalid_markers))
    if invalid_markers:
        return (
            "invalid_or_nonpaper",
            "undeclared_pdf_under_test_or_temporary_tree:" + ",".join(invalid_markers),
        )
    attachment_markers = sorted(segments & _ATTACHMENT_PATH_MARKERS)
    if attachment_markers:
        return (
            "supplement_or_attachment",
            "undeclared_pdf_under_attachment_tree:" + ",".join(attachment_markers),
        )
    return "formal_paper", "no_nonpaper_path_marker"


def _resolve(base: Path, value: Any) -> Path | None:
    text = _text(value)
    if not text:
        return None
    candidate = Path(text)
    return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()


@dataclass(frozen=True)
class RepresentationEvidence:
    representation_type: str
    role: str | None = None
    local_path: str | None = None
    url: str | None = None
    sha256: str | None = None
    availability: str | None = None
    source_id: str | None = None
    root_key: str | None = None
    declared: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelationEvidence:
    relation_type: str
    target_asset_id: str | None = None
    target_source_key: str | None = None
    confirmation_status: str | None = None
    confirmed: bool = False


@dataclass
class EvidenceRecord:
    record_key: str
    evidence_kind: str
    asset_type: str
    title: str
    year: int | None
    identifiers: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    representations: list[RepresentationEvidence] = field(default_factory=list)
    relations: list[RelationEvidence] = field(default_factory=list)
    asset_id: str | None = None
    source_id: str | None = None
    source_record_id: str | None = None
    provenance_keys: set[str] = field(default_factory=set)
    # Adapter asset attributes are first-class evidence.  Keep the complete
    # JSON object so code conclusions are not lost between source adapters,
    # catalogs and the paper-level closure report.  Consumers must still
    # recognize only the allowlisted code-status values below.
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def source_key(self) -> str | None:
        if self.source_id and self.source_record_id:
            return f"{self.source_id}:{self.source_record_id}"
        return None


@dataclass(frozen=True)
class SourceRuntime:
    source_id: str
    root: Path
    asset_roots: dict[str, str]

    def root_for(self, root_key: str | None) -> Path:
        key = root_key or "source"
        if key == "source":
            return self.root
        configured = self.asset_roots.get(key)
        if configured is None:
            return self.root
        value = Path(configured)
        return value.resolve() if value.is_absolute() else (self.root / value).resolve()

    def representation_path(self, root_key: str | None, path_ref: Any) -> str | None:
        text = _text(path_ref)
        if not text:
            return None
        return str((self.root_for(root_key) / Path(text)).resolve())


class UnionFind:
    def __init__(self, keys: Iterable[str]) -> None:
        self.parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self.parent[key]
        if parent != key:
            self.parent[key] = self.find(parent)
        return self.parent[key]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            winner, loser = sorted((left_root, right_root))
            self.parent[loser] = winner


def load_census(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != CENSUS_SCHEMA_VERSION:
        raise ValueError(f"{path} is not a {CENSUS_SCHEMA_VERSION} report")
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError(f"{path} has no records array")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError(f"census record {index} is not an object")
        record_id = _text(record.get("record_id"))
        if not record_id:
            raise ValueError(f"census record {index} has no record_id")
        if record_id in seen:
            raise ValueError(f"duplicate census record_id: {record_id}")
        seen.add(record_id)
    return value


def load_retirement_status(path: Path) -> tuple[set[str], set[str]]:
    """Load retired source identifiers/aliases from a lifecycle status report.

    The status report is an explicit, read-only boundary: only entries whose
    ``lifecycle_status`` is exactly ``retired`` are accepted.  Both the stable
    source id and the human-facing alias are returned because manifests may
    use either as ``EvidenceRecord.source_id``.
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"{path} has no sources array")
    retired: set[str] = set()
    aliases: set[str] = set()
    for index, item in enumerate(sources):
        if not isinstance(item, Mapping):
            raise ValueError(f"retirement status source {index} is not an object")
        if _text(item.get("lifecycle_status")) != "retired":
            continue
        for key, target in (("source_id", retired), ("source_alias", aliases)):
            identifier = _text(item.get(key))
            if identifier:
                target.add(identifier)
    return retired | aliases, aliases


def _resolve_source_paths(
    raw: Mapping[str, Any], *, hub_root: Path
) -> tuple[dict[str, Any], SourceRuntime]:
    source_id = _text(raw.get("id"))
    root_text = _text(raw.get("root"))
    manifest_text = _text(raw.get("manifest"))
    if not source_id or not root_text or not manifest_text:
        raise ValueError("source requires id, root, and manifest")
    root = Path(root_text)
    root = root.resolve() if root.is_absolute() else (hub_root / root).resolve()
    manifest = Path(manifest_text)
    manifest = manifest.resolve() if manifest.is_absolute() else (root / manifest).resolve()
    prepared = dict(raw)
    prepared["root"] = str(root)
    prepared["manifest"] = str(manifest)
    prepared.setdefault("label", source_id)
    prepared.setdefault("adapter", "generic_manifest")
    prepared.setdefault("read_only", True)
    runtime = SourceRuntime(
        source_id=source_id,
        root=root,
        asset_roots={str(key): str(value) for key, value in (raw.get("asset_roots") or {}).items()},
    )
    return prepared, runtime


def _normalized_record_evidence(record: Any, runtime: SourceRuntime) -> EvidenceRecord:
    asset = record.asset
    identifiers = {
        candidate
        for item in asset.external_identifiers
        if (candidate := _identifier(item.scheme, item.value)) is not None
    }
    representations: list[RepresentationEvidence] = []
    for item in record.representations:
        rep_type = _value(item.representation_type).casefold()
        local_path = runtime.representation_path(item.root_key, item.path_ref)
        representations.append(
            RepresentationEvidence(
                representation_type=rep_type,
                role=_value(item.role) or None,
                local_path=local_path,
                url=_text(item.url),
                sha256=_normalize_sha(item.sha256),
                availability=_value(item.availability) or None,
                source_id=record.source_id,
                root_key=item.root_key,
                metadata=dict(item.metadata or {}),
            )
        )
    relations: list[RelationEvidence] = []
    for item in record.relations:
        target_source_key = None
        if item.object.source_record_id:
            target_source_key = (
                f"{item.object.source_id or record.source_id}:{item.object.source_record_id}"
            )
        relations.append(
            RelationEvidence(
                relation_type=_value(item.relation_type).casefold(),
                target_source_key=target_source_key,
                confirmed=bool(item.confirmed),
                confirmation_status="confirmed" if item.confirmed else "candidate",
            )
        )
    evidence = EvidenceRecord(
        record_key=f"source:{record.source_id}:{record.source_record_id}",
        evidence_kind="source_manifest",
        asset_type=_value(asset.asset_type).casefold(),
        title=asset.title,
        year=asset.year,
        identifiers=identifiers,
        domains=set(asset.domain_slugs),
        representations=representations,
        relations=relations,
        asset_id=str(asset.asset_id),
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        attributes=dict(asset.attributes or {}),
    )
    if evidence.source_key:
        evidence.provenance_keys.add(evidence.source_key)
    return evidence


def load_sources_evidence(
    sources_yaml: Path,
    *,
    hub_root: Path | None = None,
) -> tuple[list[EvidenceRecord], dict[str, SourceRuntime], list[dict[str, str]]]:
    """Normalize all configured sources without writing source or Hub state."""

    document = yaml.safe_load(sources_yaml.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or not isinstance(document.get("sources"), list):
        raise ValueError(f"{sources_yaml} must contain a sources array")
    inferred_root = sources_yaml.resolve().parent.parent
    project_root = (hub_root or inferred_root).resolve()
    evidence: list[EvidenceRecord] = []
    runtimes: dict[str, SourceRuntime] = {}
    errors: list[dict[str, str]] = []
    for index, raw in enumerate(document["sources"]):
        if not isinstance(raw, Mapping):
            errors.append(
                {
                    "source_id": f"sources[{index}]",
                    "error_type": "InvalidSource",
                    "error": "source entry is not an object",
                }
            )
            continue
        source_id = _text(raw.get("id")) or f"sources[{index}]"
        try:
            prepared, runtime = _resolve_source_paths(raw, hub_root=project_root)
            config = SourceConfig.model_validate(prepared)
            adapter = DEFAULT_REGISTRY.create(config.adapter, config, base_dir=project_root)
            normalized = adapter.load()
            runtimes[source_id] = runtime
            evidence.extend(_normalized_record_evidence(record, runtime) for record in normalized)
        except Exception as error:  # preserve other sources as auditable evidence
            errors.append(
                {
                    "source_id": source_id,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    return evidence, runtimes, errors


def _catalog_representation(
    raw: Mapping[str, Any], runtimes: Mapping[str, SourceRuntime], document_root: Path
) -> RepresentationEvidence | None:
    rep_type = _text(raw.get("representation_type") or raw.get("type"))
    if not rep_type:
        return None
    source_id = _text(raw.get("source_alias") or raw.get("source_id"))
    root_key = _text(raw.get("root_key")) or "source"
    path_ref = raw.get("path_ref") or raw.get("path")
    if source_id and source_id in runtimes:
        local_path = runtimes[source_id].representation_path(root_key, path_ref)
    else:
        resolved = _resolve(document_root, path_ref)
        local_path = str(resolved) if resolved else None
    return RepresentationEvidence(
        representation_type=rep_type.casefold(),
        role=_text(raw.get("role")),
        local_path=local_path,
        url=_text(raw.get("external_url") or raw.get("url")),
        sha256=_normalize_sha(raw.get("sha256")),
        availability=_text(raw.get("availability")),
        source_id=source_id,
        root_key=root_key,
        metadata=(dict(raw.get("metadata")) if isinstance(raw.get("metadata"), Mapping) else {}),
    )


def _catalog_record(
    raw: Mapping[str, Any], *, runtimes: Mapping[str, SourceRuntime], document_root: Path
) -> EvidenceRecord | None:
    asset = raw.get("asset")
    if not isinstance(asset, Mapping):
        return None
    asset_id = _text(asset.get("asset_id"))
    title = _text(asset.get("title"))
    if not asset_id or not title:
        return None
    identifiers: set[str] = set()
    for item in _list(asset.get("external_identifiers")):
        if isinstance(item, Mapping):
            candidate = _identifier(item.get("scheme"), item.get("value") or item.get("canonical"))
            if candidate:
                identifiers.add(candidate)
    representations = [
        candidate
        for item in _list(raw.get("representations"))
        if isinstance(item, Mapping)
        if (candidate := _catalog_representation(item, runtimes, document_root)) is not None
    ]
    # Older catalogs kept the guide payload beside (rather than inside) the
    # representation list.  Preserve it as metadata-only guide evidence so
    # explicit no-code conclusions remain visible without inventing a path.
    guide_payload = raw.get("catalog_code_guide")
    source_payload = raw.get("source_payload")
    if guide_payload is None and isinstance(source_payload, Mapping):
        guide_payload = source_payload.get("catalog_code_guide")
    if isinstance(guide_payload, Mapping) and not any(
        item.representation_type == "guide" or (item.role or "").casefold() == "guide"
        for item in representations
    ):
        representations.append(
            RepresentationEvidence(
                representation_type="guide",
                role="guide",
                metadata={"catalog_code_guide": dict(guide_payload)},
                source_id=_text(raw.get("source_alias") or raw.get("source_id")),
            )
        )
    source_payload = raw.get("source_payload")
    payload_attributes = (
        source_payload.get("attributes")
        if isinstance(source_payload, Mapping)
        else None
    )
    payload_asset_attributes = (
        source_payload.get("asset", {}).get("attributes")
        if isinstance(source_payload, Mapping)
        and isinstance(source_payload.get("asset"), Mapping)
        else None
    )
    # Catalog exports have used all three locations over time.  Preserve the
    # source payload as a fallback, while the canonical asset attributes win
    # on key conflicts.  Status recognition remains strict and read-only.
    attributes: dict[str, Any] = {}
    for candidate in (
        payload_attributes,
        payload_asset_attributes,
        raw.get("attributes"),
        asset.get("attributes"),
    ):
        if isinstance(candidate, Mapping):
            attributes.update(candidate)
    relations: list[RelationEvidence] = []
    for item in _list(raw.get("relations")):
        if not isinstance(item, Mapping):
            continue
        status = _text(item.get("confirmation_status"))
        relations.append(
            RelationEvidence(
                relation_type=(_text(item.get("relation_type")) or "related_to").casefold(),
                target_asset_id=_text(item.get("object_asset_id")),
                confirmation_status=status,
                confirmed=status == "confirmed",
            )
        )
    provenance_keys: set[str] = set()
    provenance = raw.get("provenance")
    if isinstance(provenance, Mapping):
        for item in _list(provenance.get("source_records")):
            if not isinstance(item, Mapping):
                continue
            source_id = _text(item.get("source_alias") or item.get("source_id"))
            record_id = _text(item.get("source_record_id"))
            if source_id and record_id:
                provenance_keys.add(f"{source_id}:{record_id}")
    return EvidenceRecord(
        record_key=f"hub:{asset_id}",
        evidence_kind="hub_catalog",
        asset_type=(_text(asset.get("asset_type")) or "paper").casefold(),
        title=title,
        year=int(asset["year"]) if asset.get("year") is not None else None,
        identifiers=identifiers,
        domains={str(value) for value in _list(asset.get("domains")) if _text(value)},
        representations=representations,
        relations=relations,
        asset_id=asset_id,
        provenance_keys=provenance_keys,
        attributes=attributes,
    )


def _raw_representation(
    representation_type: str,
    raw: Any,
    *,
    document_root: Path,
) -> RepresentationEvidence | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.startswith(("http://", "https://")):
            return RepresentationEvidence(representation_type=representation_type, url=raw)
        return RepresentationEvidence(
            representation_type=representation_type,
            local_path=str(_resolve(document_root, raw)),
        )
    if not isinstance(raw, Mapping):
        return None
    path_value = raw.get("path") or raw.get("path_ref") or raw.get("local")
    url = _text(raw.get("url") or raw.get("external_url"))
    path = _resolve(document_root, path_value)
    if path is None and url is None:
        return None
    return RepresentationEvidence(
        representation_type=representation_type,
        role=_text(raw.get("role")),
        local_path=str(path) if path else None,
        url=url,
        sha256=_normalize_sha(raw.get("sha256")),
        availability=_text(raw.get("availability")),
        metadata=(dict(raw.get("metadata")) if isinstance(raw.get("metadata"), Mapping) else {}),
    )


def _heuristic_record(
    raw: Mapping[str, Any], *, document_id: str, index: int, document_root: Path
) -> EvidenceRecord | None:
    asset = raw.get("asset") if isinstance(raw.get("asset"), Mapping) else raw
    title = _text(asset.get("title"))
    if not title:
        return None
    record_id = _text(raw.get("source_record_id") or raw.get("id") or raw.get("paper_id"))
    record_id = record_id or f"record-{index}"
    source_id = _text(raw.get("source_id")) or document_id
    identifiers = set()
    for scheme, keys in (("doi", ("doi",)), ("arxiv", ("arxiv_id", "arxiv"))):
        for key in keys:
            candidate = _identifier(scheme, asset.get(key))
            if candidate:
                identifiers.add(candidate)
    for item in _list(asset.get("external_identifiers")):
        if isinstance(item, Mapping):
            candidate = _identifier(item.get("scheme"), item.get("value") or item.get("canonical"))
            if candidate:
                identifiers.add(candidate)
    representations: list[RepresentationEvidence] = []
    nested = raw.get("representations")
    if isinstance(nested, Mapping):
        for key, kind in (
            ("pdf", "pdf"),
            ("html", "html"),
            ("code", "repository"),
            ("repository", "repository"),
        ):
            candidate = _raw_representation(kind, nested.get(key), document_root=document_root)
            if candidate:
                representations.append(candidate)
    elif isinstance(nested, list):
        for item in nested:
            if not isinstance(item, Mapping):
                continue
            kind = _text(item.get("representation_type") or item.get("type"))
            if kind:
                candidate = _raw_representation(kind.casefold(), item, document_root=document_root)
                if candidate:
                    representations.append(candidate)
    top_level_specs = (
        ("pdf", ("pdf_local", "local_pdf", "local_pdf_path", "pdf_path", "pdf")),
        ("html", ("html_local", "paper_page", "paper_page_url", "html")),
        ("repository", ("repository_local", "repo_local", "local_code", "code")),
    )
    existing_kinds = {item.representation_type for item in representations}
    for kind, keys in top_level_specs:
        if kind in existing_kinds:
            continue
        for key in keys:
            candidate = _raw_representation(kind, raw.get(key), document_root=document_root)
            if candidate:
                representations.append(candidate)
                break
    for kind, keys in (
        ("pdf", ("pdf_url",)),
        ("html", ("html_url",)),
        ("repository", ("official_repo_url", "repo_url", "official_code", "official_repo")),
    ):
        urls = [_text(raw.get(key)) for key in keys]
        urls = [url for url in urls if url and url.startswith(("http://", "https://"))]
        if not urls:
            continue
        same_kind = next(
            (item for item in representations if item.representation_type == kind), None
        )
        if same_kind is None:
            representations.append(RepresentationEvidence(representation_type=kind, url=urls[0]))
        elif same_kind.url is None:
            representations.remove(same_kind)
            representations.append(
                RepresentationEvidence(
                    representation_type=same_kind.representation_type,
                    role=same_kind.role,
                    local_path=same_kind.local_path,
                    url=urls[0],
                    sha256=same_kind.sha256,
                    availability=same_kind.availability,
                    source_id=same_kind.source_id,
                    root_key=same_kind.root_key,
                    metadata=dict(same_kind.metadata),
                )
            )
    return EvidenceRecord(
        record_key=f"extra:{document_id}:{record_id}:{index}",
        evidence_kind="extra_document",
        asset_type=(_text(asset.get("asset_type")) or "paper").casefold(),
        title=title,
        year=int(asset["year"]) if asset.get("year") is not None else None,
        identifiers=identifiers,
        domains={
            str(value)
            for value in _list(asset.get("domain_slugs") or asset.get("domains"))
            if _text(value)
        },
        representations=representations,
        source_id=source_id,
        source_record_id=record_id,
        provenance_keys={f"{source_id}:{record_id}"},
        attributes=(
            dict(asset.get("attributes"))
            if isinstance(asset.get("attributes"), Mapping)
            else {}
        ),
    )


def _iter_document_records(path: Path) -> tuple[list[Any], dict[str, Any]]:
    metadata: dict[str, Any] = {"path": str(path.resolve())}
    if path.suffix.casefold() == ".jsonl":
        records = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid JSONL at {path}:{line_number}: {error}") from error
        metadata["record_selector"] = "jsonl"
        return records, metadata
    if path.suffix.casefold() in {".yaml", ".yml"}:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        metadata["record_selector"] = "$"
        return document, metadata
    if not isinstance(document, Mapping):
        raise ValueError(f"unsupported document root in {path}")
    for selector in ("items", "papers", "records", "assets"):
        selected = document.get(selector)
        if isinstance(selected, list):
            metadata["record_selector"] = selector
            metadata["generated_at"] = document.get("generated_at")
            return selected, metadata
    if isinstance(document.get("asset"), Mapping):
        metadata["record_selector"] = "$"
        return [document], metadata
    raise ValueError(f"no records/items/papers/assets array in {path}")


def load_document_evidence(
    paths: Sequence[Path],
    *,
    runtimes: Mapping[str, SourceRuntime],
) -> tuple[list[EvidenceRecord], list[dict[str, Any]], list[dict[str, str]]]:
    evidence: list[EvidenceRecord] = []
    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            raw_records, metadata = _iter_document_records(path)
            documents.append({**metadata, "record_count": len(raw_records)})
            document_id = (
                f"{path.stem}-{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]}"
            )
            for index, raw in enumerate(raw_records):
                if not isinstance(raw, Mapping):
                    continue
                catalog = _catalog_record(raw, runtimes=runtimes, document_root=path.parent)
                if catalog is not None:
                    evidence.append(catalog)
                    continue
                heuristic = _heuristic_record(
                    raw,
                    document_id=document_id,
                    index=index,
                    document_root=path.parent,
                )
                if heuristic is not None:
                    evidence.append(heuristic)
        except Exception as error:
            errors.append(
                {
                    "source_id": str(path.resolve()),
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    return evidence, documents, errors


def load_no_code_titles(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        return set()
    titles: set[str] = set()
    for value in document.values():
        if not isinstance(value, Mapping):
            continue
        for title in _list(value.get("no_code_titles")):
            text = _text(title)
            if text:
                titles.add(text)
    return titles


def _union_by_key(
    union: UnionFind,
    records: Sequence[EvidenceRecord],
    values: Iterable[tuple[str, str]],
) -> None:
    first: dict[str, str] = {}
    for record_key, identity in values:
        previous = first.setdefault(identity, record_key)
        union.union(previous, record_key)


def _build_components(records: Sequence[EvidenceRecord]) -> dict[str, list[EvidenceRecord]]:
    union = UnionFind(record.record_key for record in records)
    _union_by_key(
        union,
        records,
        (
            (record.record_key, identifier)
            for record in records
            for identifier in record.identifiers
        ),
    )
    _union_by_key(
        union,
        records,
        (
            (record.record_key, f"pdf-sha256:{item.sha256}")
            for record in records
            for item in record.representations
            if item.representation_type in PDF_TYPES and item.sha256
        ),
    )
    asset_keys = {record.asset_id: record.record_key for record in records if record.asset_id}
    source_keys: dict[str, str] = {}
    for record in records:
        if record.source_key:
            source_keys.setdefault(record.source_key, record.record_key)
    for record in records:
        for key in record.provenance_keys:
            target = source_keys.get(key)
            if target:
                union.union(record.record_key, target)
        for relation in record.relations:
            if relation.relation_type not in IDENTITY_RELATIONS:
                continue
            if relation.target_asset_id and relation.target_asset_id in asset_keys:
                union.union(record.record_key, asset_keys[relation.target_asset_id])
            if relation.target_source_key and relation.target_source_key in source_keys:
                union.union(record.record_key, source_keys[relation.target_source_key])
    components: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        components[union.find(record.record_key)].append(record)
    return components


def _canonical_id(members: Sequence[EvidenceRecord]) -> str:
    identifiers = sorted({value for member in members for value in member.identifiers})
    for prefix in ("doi:", "arxiv:"):
        matches = [value for value in identifiers if value.startswith(prefix)]
        if matches:
            return matches[0]
    pdf_hashes = sorted(
        {
            item.sha256
            for member in members
            for item in member.representations
            if item.representation_type in PDF_TYPES and item.sha256
        }
    )
    if pdf_hashes:
        return f"sha256:{pdf_hashes[0]}"
    asset_ids = sorted(member.asset_id for member in members if member.asset_id)
    if asset_ids:
        return f"hub:{asset_ids[0]}"
    source_keys = sorted(member.source_key for member in members if member.source_key)
    seed = source_keys[0] if source_keys else min(member.record_key for member in members)
    return f"evidence:{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


def _candidate_summary(canonical_id: str, members: Sequence[EvidenceRecord]) -> dict[str, Any]:
    papers = [member for member in members if member.asset_type == "paper"] or list(members)
    preferred = next(
        (member for member in papers if member.evidence_kind == "hub_catalog"), papers[0]
    )
    identifiers = sorted({value for member in members for value in member.identifiers})
    return {
        "canonical_id": canonical_id,
        "title": preferred.title,
        "year": preferred.year,
        "identifiers": identifiers,
        "domains": sorted({value for member in members for value in member.domains}),
        "hub_asset_ids": sorted(
            {
                member.asset_id
                for member in members
                if member.evidence_kind == "hub_catalog" and member.asset_id
            }
        ),
        "source_keys": sorted({member.source_key for member in members if member.source_key}),
        "evidence_records": sorted(member.record_key for member in members),
    }


def _serving_members(
    canonical_members: Mapping[str, list[EvidenceRecord]],
    retired_sources: set[str],
) -> tuple[dict[str, list[EvidenceRecord]], dict[str, dict[str, Any]]]:
    """Remove retired source evidence from serving closure while retaining history.

    Canonical IDs and identity indexes are built from the complete evidence set;
    only representation aggregation uses this filtered map.  A canonical made
    solely of retired evidence is left intact so its historical row remains
    visible and auditable.
    """

    active: dict[str, list[EvidenceRecord]] = {}
    historical: dict[str, dict[str, Any]] = {}
    for canonical_id, members in canonical_members.items():
        retired_members = [
            member
            for member in members
            if member.source_id is not None and member.source_id in retired_sources
        ]
        if retired_members:
            aliases = sorted(
                {
                    member.source_id
                    for member in retired_members
                    if member.source_id is not None
                }
            )
            historical[canonical_id] = {
                "historical_retired_count": len(retired_members),
                "historical_retired_aliases": aliases,
            }
        serving = [member for member in members if member not in retired_members]
        active[canonical_id] = serving or list(members)
    return active, historical


def _paths_for(
    members: Sequence[EvidenceRecord], kinds: frozenset[str]
) -> tuple[list[str], list[str], list[str]]:
    local_paths = sorted(
        {
            item.local_path
            for member in members
            for item in member.representations
            if item.representation_type in kinds and item.local_path
        }
    )
    existing = sorted(path for path in local_paths if Path(path).exists())
    urls = sorted(
        {
            item.url
            for member in members
            for item in member.representations
            if item.representation_type in kinds and item.url
        }
    )
    return local_paths, existing, urls


def _relation_status(
    members: Sequence[EvidenceRecord], records_by_asset: Mapping[str, EvidenceRecord]
) -> tuple[str, list[str], list[str]]:
    confirmed: list[str] = []
    candidates: list[str] = []
    declared: list[str] = []
    for member in members:
        if member.asset_type != "paper":
            continue
        for relation in member.relations:
            if relation.relation_type != "implemented_by":
                continue
            target = relation.target_asset_id or relation.target_source_key or "unknown"
            if member.evidence_kind != "hub_catalog":
                declared.append(target)
            elif relation.confirmed or relation.confirmation_status == "confirmed":
                confirmed.append(target)
            else:
                candidates.append(target)
    if confirmed:
        return "confirmed", sorted(set(confirmed)), sorted(set(candidates))
    if candidates:
        return "candidate", [], sorted(set(candidates))
    has_catalog_paper = any(
        member.asset_type == "paper" and member.evidence_kind == "hub_catalog" for member in members
    )
    has_repository = any(
        item.representation_type in CODE_TYPES
        for member in members
        for item in member.representations
    )
    if has_repository and has_catalog_paper:
        return "missing", [], []
    if declared or has_repository:
        return "declared_unverified", [], sorted(set(declared))
    return "no_code_evidence", [], []


def _attached_repository_representations(
    members: Sequence[EvidenceRecord], records_by_asset: Mapping[str, EvidenceRecord]
) -> list[RepresentationEvidence]:
    result = [
        item
        for member in members
        for item in member.representations
        if item.representation_type in CODE_TYPES
    ]
    for member in members:
        for relation in member.relations:
            if relation.relation_type != "implemented_by" or not relation.target_asset_id:
                continue
            target = records_by_asset.get(relation.target_asset_id)
            if target is None:
                continue
            result.extend(
                item for item in target.representations if item.representation_type in CODE_TYPES
            )
            # Repository catalog assets often expose the official URL as an asset source URL
            # through a representation; no guessed URL is manufactured here.
    return result


def _title_candidates(
    title_values: Sequence[str],
    year: int | None,
    candidates: Mapping[str, dict[str, Any]],
    *,
    threshold: float,
    limit: int = 8,
) -> list[dict[str, Any]]:
    queries = {
        _normalize_title(value) for value in title_values if len(_normalize_title(value)) >= 10
    }
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for canonical_id, summary in candidates.items():
        target = _normalize_title(summary.get("title"))
        if len(target) < 10:
            continue
        candidate_year = summary.get("year")
        if year is not None and candidate_year is not None and year != candidate_year:
            continue
        score = max(
            (SequenceMatcher(None, query, target).ratio() for query in queries), default=0.0
        )
        if score >= threshold:
            ranked.append((score, canonical_id, summary))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "canonical_id": canonical_id,
            "title": summary["title"],
            "year": summary["year"],
            "score": round(score, 6),
            "review_required": True,
            "auto_merge": False,
        }
        for score, canonical_id, summary in ranked[:limit]
    ]


def _unique_representations(
    items: Sequence[RepresentationEvidence],
) -> list[RepresentationEvidence]:
    result: list[RepresentationEvidence] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        key = (item.representation_type, item.local_path, item.url, item.sha256)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _explicit_code_conclusion(
    members: Sequence[EvidenceRecord],
) -> tuple[str | None, str | None, bool]:
    """Return a strict explicit code conclusion and its reason.

    Source adapters put code review fields on ``asset.attributes`` while older
    catalogs put equivalent values in guide metadata or ``source_payload``.
    All of those channels are carried into :class:`EvidenceRecord`, but only
    known status values can close the code gate.  Conflicting explicit status
    values fail closed and leave the paper reviewable.
    """

    observations: list[tuple[str, str | None]] = []

    def add(value: Any, reason: Any = None) -> None:
        status = _text(value)
        if status is None:
            return
        normalized = status.casefold().replace("-", "_").replace(" ", "_")
        mapped = EXPLICIT_CODE_STATUS_MAP.get(normalized)
        if mapped:
            observations.append((mapped, _text(reason)))

    for member in members:
        attributes = member.attributes
        if isinstance(attributes, Mapping):
            add(attributes.get("code_status"), attributes.get("no_code_reason"))
            # A few legacy payloads named this field no_code_status.  Keep it
            # narrowly scoped; generic ``status`` is intentionally ignored.
            add(attributes.get("no_code_status"), attributes.get("no_code_reason"))
        for item in member.representations:
            if item.representation_type != "guide" and (item.role or "").casefold() != "guide":
                continue
            metadata = item.metadata
            add(
                metadata.get("status") if isinstance(metadata, Mapping) else None,
                metadata.get("no_code_reason") if isinstance(metadata, Mapping) else None,
            )
            add(
                metadata.get("code_status") if isinstance(metadata, Mapping) else None,
                metadata.get("no_code_reason") if isinstance(metadata, Mapping) else None,
            )
            nested = metadata.get("catalog_code_guide") if isinstance(metadata, Mapping) else None
            if isinstance(nested, Mapping):
                add(nested.get("status"), nested.get("no_code_reason") or nested.get("reason"))
                add(
                    nested.get("code_status"),
                    nested.get("no_code_reason") or nested.get("reason"),
                )

    statuses = {status for status, _ in observations}
    if len(statuses) != 1:
        return None, None, len(statuses) > 1
    status = next(iter(statuses))
    reason = next((reason for value, reason in observations if value == status and reason), None)
    return status, reason, False


def _explicit_code_guide_status(
    members: Sequence[EvidenceRecord],
) -> str | None:
    """Backward-compatible status-only view of explicit code evidence."""

    status, _reason, conflict = _explicit_code_conclusion(members)
    return None if conflict else status


def _record_closure(
    census_record: Mapping[str, Any],
    *,
    core_root: Path,
    batch_root: Path | None,
    batch_additional_roots: Sequence[Path],
    canonical_members: Mapping[str, list[EvidenceRecord]],
    canonical_summaries: Mapping[str, dict[str, Any]],
    sha_index: Mapping[str, set[str]],
    path_index: Mapping[str, set[str]],
    identifier_index: Mapping[str, set[str]],
    records_by_asset: Mapping[str, EvidenceRecord],
    no_code_titles: set[str],
    weak_title_threshold: float,
    retirement_by_canonical: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    absolute_path = _text(census_record.get("absolute_path")) or ""
    relative_path = _text(census_record.get("relative_path")) or ""
    sha256 = _normalize_sha(census_record.get("sha256"))
    pdf = census_record.get("pdf") if isinstance(census_record.get("pdf"), Mapping) else {}
    metadata_title = _text(pdf.get("metadata_title"))
    filename_title = _title_from_filename(relative_path or absolute_path)
    inferred_year = _infer_year(relative_path, metadata_title)
    identifiers = _identifiers_from_text(relative_path, absolute_path, metadata_title)
    strong_matches: set[str] = set()
    match_basis: dict[str, set[str]] = defaultdict(set)
    if sha256:
        for canonical_id in sha_index.get(sha256, set()):
            strong_matches.add(canonical_id)
            match_basis[canonical_id].add("pdf_sha256")
    if absolute_path:
        for canonical_id in path_index.get(_norm_path(absolute_path), set()):
            strong_matches.add(canonical_id)
            match_basis[canonical_id].add("declared_local_path")
    for identifier in identifiers:
        for canonical_id in identifier_index.get(identifier, set()):
            strong_matches.add(canonical_id)
            match_basis[canonical_id].add(identifier.split(":", 1)[0])

    weak_candidates = _title_candidates(
        [value for value in (metadata_title, filename_title) if value],
        inferred_year,
        canonical_summaries,
        threshold=weak_title_threshold,
    )
    canonical_id = next(iter(strong_matches)) if len(strong_matches) == 1 else None
    if len(strong_matches) > 1:
        match_status = "conflict"
    elif canonical_id is not None:
        basis = match_basis[canonical_id]
        match_status = "matched_strong" if basis - {"declared_local_path"} else "matched_declared"
    elif weak_candidates:
        match_status = "weak_candidate_only"
    else:
        match_status = "unmatched"

    root_in_core = bool(census_record.get("root_in_core"))
    if absolute_path:
        root_in_core = bool(_path_is_within(absolute_path, core_root))
    batch_member = bool(absolute_path and batch_root and _path_is_within(absolute_path, batch_root))
    acquisition_batch_member = bool(
        batch_member or any(_path_is_within(absolute_path, root) for root in batch_additional_roots)
    )
    parse_status = _text(pdf.get("parse_status"))
    pdf_valid = bool(pdf.get("magic_ok")) and parse_status in {"ok", "encrypted_empty_password"}
    exact_declared_pdf_path = any(
        "declared_local_path" in match_basis[canonical] for canonical in strong_matches
    )
    classification, classification_reason = _classify_census_pdf(
        path_for_classification=relative_path or absolute_path,
        in_acquisition_batch=acquisition_batch_member,
        exact_declared_pdf_path=exact_declared_pdf_path,
    )

    unresolved: list[str] = []
    actions: list[str] = []
    if not pdf_valid:
        unresolved.append("pdf_invalid_or_unparsed")
        actions.append("repair_or_replace_pdf")
    if not root_in_core:
        unresolved.append("pdf_not_in_core")
        actions.append("copy_or_move_pdf_into_core")
    if match_status == "conflict":
        unresolved.append("strong_identity_conflict")
        actions.append("resolve_strong_identity_conflict")
    elif match_status == "weak_candidate_only":
        unresolved.append("weak_title_candidate_only")
        actions.append("manually_review_identity")
    elif match_status == "unmatched":
        unresolved.append("hub_record_missing")
        actions.append("assign_domain_and_register_hub_record")

    closure_pdf: dict[str, Any] = {
        "registered": False,
        "declared_paths": [],
        "existing_paths": [],
        "urls": [],
        "all_existing_paths_in_core": None,
        "equivalent_core_copy": False,
    }
    closure_html: dict[str, Any] = {
        "status": "unmatched",
        "declared_paths": [],
        "existing_paths": [],
        "urls": [],
        "all_existing_paths_in_core": None,
    }
    closure_code: dict[str, Any] = {
        "status": "unmatched",
        "declared_paths": [],
        "existing_paths": [],
        "urls": [],
        "all_existing_paths_in_core": None,
        "no_code_allowlisted": False,
        "guide_status": None,
        "explicit_status": None,
        "explicit_no_code_reason": None,
        "explicit_status_conflict": False,
    }
    relation_status = "unmatched"
    confirmed_targets: list[str] = []
    candidate_targets: list[str] = []
    domains: list[str] = []
    canonical_summary = None

    if canonical_id is not None:
        members = canonical_members[canonical_id]
        canonical_summary = canonical_summaries[canonical_id]
        domains = canonical_summary["domains"]
        equivalent_core_copy = bool(
            sha256
            and any(
                item.representation_type in PDF_TYPES
                and _normalize_sha(item.sha256) == sha256
                and item.local_path
                and Path(item.local_path).is_file()
                and _path_is_within(item.local_path, core_root)
                for member in members
                for item in member.representations
            )
        )
        pdf_paths, pdf_existing, pdf_urls = _paths_for(members, PDF_TYPES)
        html_paths, html_existing, html_urls = _paths_for(members, HTML_TYPES)
        code_items = _unique_representations(
            _attached_repository_representations(members, records_by_asset)
        )
        guide_status, explicit_reason, explicit_conflict = _explicit_code_conclusion(members)
        code_paths = sorted({item.local_path for item in code_items if item.local_path})
        code_existing = sorted(path for path in code_paths if Path(path).exists())
        code_urls = sorted({item.url for item in code_items if item.url})
        closure_pdf = {
            "registered": bool(pdf_paths or pdf_urls),
            "declared_paths": pdf_paths,
            "existing_paths": pdf_existing,
            "urls": pdf_urls,
            "all_existing_paths_in_core": (
                all(bool(_path_is_within(path, core_root)) for path in pdf_existing)
                if pdf_existing
                else None
            ),
            "equivalent_core_copy": equivalent_core_copy,
        }
        if equivalent_core_copy:
            unresolved = [item for item in unresolved if item != "pdf_not_in_core"]
            actions = [item for item in actions if item != "copy_or_move_pdf_into_core"]
        closure_html = {
            "status": "local" if html_existing else ("url_only" if html_urls else "missing"),
            "declared_paths": html_paths,
            "existing_paths": html_existing,
            "urls": html_urls,
            "all_existing_paths_in_core": (
                all(bool(_path_is_within(path, core_root)) for path in html_existing)
                if html_existing
                else None
            ),
        }
        allowlisted = canonical_summary["title"] in no_code_titles
        # A materialized repository is authoritative even when a stale guide
        # still says ``announced_pending`` or ``third_party_only``.
        if code_existing and code_urls:
            code_status = "local_and_url"
        elif code_existing:
            code_status = "local_only"
        elif guide_status:
            code_status = guide_status
        elif allowlisted:
            code_status = "allowlisted_no_code"
        elif code_urls:
            code_status = "url_only"
        else:
            code_status = "missing_or_unreviewed"
        closure_code = {
            "status": code_status,
            "declared_paths": code_paths,
            "existing_paths": code_existing,
            "urls": code_urls,
            "all_existing_paths_in_core": (
                all(bool(_path_is_within(path, core_root)) for path in code_existing)
                if code_existing
                else None
            ),
            "no_code_allowlisted": allowlisted,
            "guide_status": guide_status,
            "explicit_status": guide_status,
            "explicit_no_code_reason": explicit_reason,
            "explicit_status_conflict": explicit_conflict,
        }
        relation_status, confirmed_targets, candidate_targets = _relation_status(
            members, records_by_asset
        )

        if not closure_pdf["registered"]:
            unresolved.append("pdf_not_registered_in_hub")
            actions.append("register_pdf_representation")
        if closure_pdf["declared_paths"] and not closure_pdf["existing_paths"]:
            unresolved.append("registered_pdf_path_missing")
            actions.append("repair_registered_pdf_path")
        if closure_pdf["all_existing_paths_in_core"] is False:
            unresolved.append("registered_pdf_not_in_core")
            actions.append("consolidate_registered_pdf_into_core")
        if closure_html["status"] != "local":
            unresolved.append("html_local_missing")
            actions.append("create_or_register_local_html")
        elif closure_html["all_existing_paths_in_core"] is False:
            unresolved.append("html_not_in_core")
            actions.append("consolidate_html_into_core")
        if code_status == "missing_or_unreviewed":
            unresolved.append("official_code_review_required")
            actions.append("research_official_code_or_allowlist_exact_title")
        elif code_status == "url_only":
            unresolved.append("official_code_not_local")
            actions.append("clone_official_code_into_core")
        elif code_status == "local_only":
            unresolved.append("official_code_url_missing")
            actions.append("verify_and_record_official_code_url")
        if explicit_conflict and not code_existing:
            unresolved.append("official_code_evidence_conflict")
            actions.append("resolve_conflicting_code_status_evidence")
        if closure_code["all_existing_paths_in_core"] is False:
            unresolved.append("code_not_in_core")
            actions.append("consolidate_code_into_core")
        relation_exempt_statuses = {
            "allowlisted_no_code",
            "missing_or_unreviewed",
            *EXPLICIT_CODE_STATUS_MAP.values(),
        }
        if code_status not in relation_exempt_statuses:
            if relation_status == "missing":
                unresolved.append("hub_implemented_by_missing")
                actions.append("reconcile_implemented_by_relation")
            elif relation_status in {"candidate", "declared_unverified"}:
                unresolved.append("hub_implemented_by_unverified")
                actions.append("verify_hub_implemented_by_relation")

    unresolved = list(dict.fromkeys(unresolved))
    actions = list(dict.fromkeys(actions))
    if classification != "formal_paper":
        # The census row is still emitted, including its match evidence, but it
        # is not a request to create a duplicate Hub paper/HTML/code record.
        unresolved = []
        actions = []
        disposition = classification
    elif "pdf_invalid_or_unparsed" in unresolved:
        disposition = "repair_or_replace_pdf"
    elif "strong_identity_conflict" in unresolved:
        disposition = "manual_identity_resolution"
    elif "weak_title_candidate_only" in unresolved:
        disposition = "manual_identity_review"
    elif "pdf_not_in_core" in unresolved:
        disposition = "relocate_into_core"
    elif "hub_record_missing" in unresolved:
        disposition = "register_in_hub"
    elif "html_local_missing" in unresolved:
        disposition = "create_html"
    elif "official_code_review_required" in unresolved:
        disposition = "research_official_code"
    elif "hub_implemented_by_missing" in unresolved:
        disposition = "reconcile_hub_relation"
    elif unresolved:
        disposition = "repair_closure"
    else:
        disposition = "closed"

    historical = dict(
        (retirement_by_canonical or {}).get(canonical_id, {}) if canonical_id else {}
    )
    historical.setdefault("historical_retired_count", 0)
    historical.setdefault("historical_retired_aliases", [])
    return {
        "census_record_id": census_record.get("record_id"),
        "root_id": census_record.get("root_id"),
        "root_path": census_record.get("root_path"),
        "relative_path": relative_path,
        "absolute_path": absolute_path,
        "sha256": sha256,
        "root_in_core": root_in_core,
        "in_desktop_batch": batch_member,
        "classification": classification,
        "classification_reason": classification_reason,
        "pdf_health": {
            "valid": pdf_valid,
            "magic_ok": pdf.get("magic_ok"),
            "parse_status": parse_status,
            "encrypted": pdf.get("encrypted"),
            "page_count": pdf.get("page_count"),
            "metadata_title": metadata_title,
        },
        "extracted_identifiers": sorted(identifiers),
        "canonical_match": {
            "status": match_status,
            "canonical_id": canonical_id,
            "match_basis": sorted(match_basis.get(canonical_id, set())) if canonical_id else [],
            "conflicting_canonical_ids": sorted(strong_matches) if len(strong_matches) > 1 else [],
            "candidate": canonical_summary,
        },
        "weak_title_candidates": weak_candidates,
        "domains": domains,
        "disposition": disposition,
        "required_actions": actions,
        "pdf": closure_pdf,
        "html": closure_html,
        "code": closure_code,
        "hub_relation": {
            "implemented_by_status": relation_status,
            "confirmed_target_asset_ids": confirmed_targets,
            "candidate_target_asset_ids": candidate_targets,
        },
        "retirement": {
            "historical_retired_count": int(historical.get("historical_retired_count", 0)),
            "historical_retired_aliases": sorted(
                str(value) for value in historical.get("historical_retired_aliases", [])
            ),
            "serving_active": not bool(historical.get("historical_retired_count", 0)),
        },
        "unresolved_flags": unresolved,
    }


def audit_closure(
    census: Mapping[str, Any],
    evidence: Sequence[EvidenceRecord],
    *,
    core_root: Path,
    batch_root: Path | None = None,
    batch_additional_roots: Sequence[Path] = (),
    batch_expected_current: int = 336,
    batch_expected_target: int = 376,
    no_code_titles: set[str] | None = None,
    weak_title_threshold: float = 0.94,
    source_errors: Sequence[Mapping[str, Any]] = (),
    input_documents: Sequence[Mapping[str, Any]] = (),
    retired_sources: set[str] | None = None,
    retirement_status_path: Path | None = None,
) -> dict[str, Any]:
    if census.get("schema_version") != CENSUS_SCHEMA_VERSION:
        raise ValueError(f"census schema must be {CENSUS_SCHEMA_VERSION}")
    if not 0.5 <= weak_title_threshold <= 1.0:
        raise ValueError("weak_title_threshold must be between 0.5 and 1.0")
    records = list(evidence)
    components = _build_components(records)
    canonical_members: dict[str, list[EvidenceRecord]] = {}
    for members in components.values():
        if not any(member.asset_type == "paper" for member in members):
            continue
        canonical_members[_canonical_id(members)] = members
    loaded_retired_sources = set(retired_sources or set())
    retirement_status_document = None
    if retirement_status_path is not None:
        loaded, _aliases = load_retirement_status(retirement_status_path)
        loaded_retired_sources.update(loaded)
        retirement_status_document = str(retirement_status_path.resolve())
    serving_members, retirement_by_canonical = _serving_members(
        canonical_members, loaded_retired_sources
    )
    serving_summaries = {
        canonical_id: _candidate_summary(canonical_id, members)
        for canonical_id, members in serving_members.items()
    }
    sha_index: dict[str, set[str]] = defaultdict(set)
    path_index: dict[str, set[str]] = defaultdict(set)
    identifier_index: dict[str, set[str]] = defaultdict(set)
    for canonical_id, members in canonical_members.items():
        for member in members:
            for identifier in member.identifiers:
                identifier_index[identifier].add(canonical_id)
            for item in member.representations:
                if item.representation_type in PDF_TYPES:
                    if item.sha256:
                        sha_index[item.sha256].add(canonical_id)
                    if item.local_path:
                        path_index[_norm_path(item.local_path)].add(canonical_id)
    records_by_asset = {record.asset_id: record for record in records if record.asset_id}
    pdf_records = [
        record
        for record in census.get("records", [])
        if isinstance(record, Mapping) and record.get("kind") == "pdf"
    ]
    resolved_batch_root = batch_root.resolve() if batch_root else None
    resolved_additional_roots = [path.resolve() for path in batch_additional_roots]
    closures = [
        _record_closure(
            record,
            core_root=core_root.resolve(),
            batch_root=resolved_batch_root,
            batch_additional_roots=resolved_additional_roots,
            canonical_members=serving_members,
            canonical_summaries=serving_summaries,
            sha_index=sha_index,
            path_index=path_index,
            identifier_index=identifier_index,
            records_by_asset=records_by_asset,
            no_code_titles=no_code_titles or set(),
            weak_title_threshold=weak_title_threshold,
            retirement_by_canonical=retirement_by_canonical,
        )
        for record in pdf_records
    ]
    closures.sort(
        key=lambda item: (str(item["absolute_path"]).casefold(), item["census_record_id"])
    )
    for record in closures:
        absolute_path = Path(str(record["absolute_path"]))
        record["in_batch_scope"] = bool(
            record["in_desktop_batch"]
            or any(_path_is_within(absolute_path, root) for root in resolved_additional_roots)
        )
    batch_records = [record for record in closures if record["in_batch_scope"]]
    batch_hashes = [record["sha256"] for record in batch_records if record["sha256"]]
    disposition_counts = Counter(record["disposition"] for record in closures)
    classification_counts = Counter(record["classification"] for record in closures)
    formal_records = [record for record in closures if record["classification"] == "formal_paper"]
    flag_counts = Counter(flag for record in closures for flag in record["unresolved_flags"])
    matched_counts = Counter(record["canonical_match"]["status"] for record in closures)
    historical_records = [
        record
        for record in closures
        if (record.get("retirement") or {}).get("historical_retired_count", 0)
    ]
    historical_aliases = sorted(
        {
            alias
            for record in historical_records
            for alias in (record.get("retirement") or {}).get(
                "historical_retired_aliases", []
            )
        }
    )
    source_errors_list = [dict(item) for item in source_errors]
    census_complete = bool((census.get("scope") or {}).get("complete"))
    report_status = "passed"
    if not census_complete or source_errors_list:
        report_status = "incomplete_evidence"
    elif flag_counts:
        report_status = "action_required"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "report_type": "paper_level_closure_audit",
        "read_only": True,
        "writes_to_sources_or_hub": False,
        "status": report_status,
        "inputs": {
            "census_schema": census.get("schema_version"),
            "census_scope_complete": census_complete,
            "core_root": str(core_root.resolve()),
            "batch_root": str(resolved_batch_root) if resolved_batch_root else None,
            "batch_additional_roots": [str(path) for path in resolved_additional_roots],
            "retirement_status": retirement_status_document,
            "retired_source_count": len(loaded_retired_sources),
            "evidence_record_count": len(records),
            "documents": [dict(item) for item in input_documents],
        },
        "identity_policy": {
            "auto_candidate_evidence": [
                "doi",
                "arxiv",
                "pdf_sha256",
                "declared_local_path",
                "hub_provenance",
                "same_as",
                "version_of",
            ],
            "weak_title_auto_merge": False,
            "weak_title_threshold": weak_title_threshold,
        },
        "batch_check": {
            "enabled": resolved_batch_root is not None or bool(resolved_additional_roots),
            "roots": [
                *([str(resolved_batch_root)] if resolved_batch_root else []),
                *[str(path) for path in resolved_additional_roots],
            ],
            "pdf_count": len(batch_records),
            "unique_sha256_count": len(set(batch_hashes)),
            "duplicate_sha256_count": len(batch_hashes) - len(set(batch_hashes)),
            "expected_current_count": batch_expected_current,
            "current_count_matches": len(batch_records) == batch_expected_current,
            "expected_target_count": batch_expected_target,
            "target_reached": len(batch_records) == batch_expected_target,
            "remaining_to_target": max(0, batch_expected_target - len(batch_records)),
            "over_target_by": max(0, len(batch_records) - batch_expected_target),
        },
        "hub_evidence": {
            "canonical_candidate_count": len(canonical_members),
            "hub_catalog_record_count": sum(
                record.evidence_kind == "hub_catalog" for record in records
            ),
            "source_manifest_record_count": sum(
                record.evidence_kind == "source_manifest" for record in records
            ),
            "source_errors": source_errors_list,
        },
        "stats": {
            "pdf_count": len(closures),
            "formal_paper_count": len(formal_records),
            "classified_nonpaper_count": len(closures) - len(formal_records),
            "closed_count": sum(record["disposition"] == "closed" for record in formal_records),
            "action_required_count": sum(
                record["disposition"] != "closed" for record in formal_records
            ),
            "by_disposition": dict(sorted(disposition_counts.items())),
            "by_classification": dict(sorted(classification_counts.items())),
            "by_match_status": dict(sorted(matched_counts.items())),
            "unresolved_flags": dict(sorted(flag_counts.items())),
            "historical_retired_count": len(historical_records),
            "historical_retired_aliases": historical_aliases,
            "serving_active_count": len(closures) - len(historical_records),
        },
        "records": closures,
    }


CSV_FIELDS = (
    "census_record_id",
    "absolute_path",
    "relative_path",
    "sha256",
    "root_in_core",
    "in_desktop_batch",
    "in_batch_scope",
    "pdf_valid",
    "page_count",
    "metadata_title",
    "classification",
    "classification_reason",
    "canonical_match_status",
    "canonical_id",
    "canonical_title",
    "domains",
    "disposition",
    "pdf_registered",
    "pdf_urls",
    "pdf_local_paths",
    "pdf_paths_in_core",
    "html_status",
    "html_urls",
    "html_local_paths",
    "html_paths_in_core",
    "code_status",
    "code_urls",
    "local_code_paths",
    "code_paths_in_core",
    "hub_implemented_by_status",
    "historical_retired_count",
    "historical_retired_aliases",
    "serving_active",
    "required_actions",
    "unresolved_flags",
    "weak_title_candidates",
)


def _csv_rows(records: Sequence[Mapping[str, Any]]) -> Iterable[dict[str, Any]]:
    for record in records:
        canonical = record.get("canonical_match") or {}
        candidate = canonical.get("candidate") or {}
        pdf = record.get("pdf") or {}
        html = record.get("html") or {}
        code = record.get("code") or {}
        relation = record.get("hub_relation") or {}
        health = record.get("pdf_health") or {}
        yield {
            "census_record_id": record.get("census_record_id"),
            "absolute_path": record.get("absolute_path"),
            "relative_path": record.get("relative_path"),
            "sha256": record.get("sha256"),
            "root_in_core": record.get("root_in_core"),
            "in_desktop_batch": record.get("in_desktop_batch"),
            "in_batch_scope": record.get("in_batch_scope"),
            "pdf_valid": health.get("valid"),
            "page_count": health.get("page_count"),
            "metadata_title": health.get("metadata_title"),
            "classification": record.get("classification"),
            "classification_reason": record.get("classification_reason"),
            "canonical_match_status": canonical.get("status"),
            "canonical_id": canonical.get("canonical_id"),
            "canonical_title": candidate.get("title"),
            "domains": " | ".join(record.get("domains") or []),
            "disposition": record.get("disposition"),
            "pdf_registered": pdf.get("registered"),
            "pdf_urls": " | ".join(pdf.get("urls") or []),
            "pdf_local_paths": " | ".join(pdf.get("existing_paths") or []),
            "pdf_paths_in_core": pdf.get("all_existing_paths_in_core"),
            "html_status": html.get("status"),
            "html_urls": " | ".join(html.get("urls") or []),
            "html_local_paths": " | ".join(html.get("existing_paths") or []),
            "html_paths_in_core": html.get("all_existing_paths_in_core"),
            "code_status": code.get("status"),
            "code_urls": " | ".join(code.get("urls") or []),
            "local_code_paths": " | ".join(code.get("existing_paths") or []),
            "code_paths_in_core": code.get("all_existing_paths_in_core"),
            "hub_implemented_by_status": relation.get("implemented_by_status"),
            "historical_retired_count": (record.get("retirement") or {}).get(
                "historical_retired_count", 0
            ),
            "historical_retired_aliases": " | ".join(
                (record.get("retirement") or {}).get("historical_retired_aliases", [])
            ),
            "serving_active": (record.get("retirement") or {}).get("serving_active", True),
            "required_actions": " | ".join(record.get("required_actions") or []),
            "unresolved_flags": " | ".join(record.get("unresolved_flags") or []),
            "weak_title_candidates": " | ".join(
                candidate.get("canonical_id", "")
                for candidate in record.get("weak_title_candidates") or []
            ),
        }


def _markdown(report: Mapping[str, Any]) -> str:
    stats = report.get("stats") or {}
    batch = report.get("batch_check") or {}
    lines = [
        "# Paper-level closure audit",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- PDF records: `{stats.get('pdf_count', 0)}`",
        f"- Formal papers: `{stats.get('formal_paper_count', 0)}`",
        f"- Explicit non-paper/repository PDFs: `{stats.get('classified_nonpaper_count', 0)}`",
        f"- Closed: `{stats.get('closed_count', 0)}`",
        f"- Action required: `{stats.get('action_required_count', 0)}`",
        f"- Historical retired records: `{stats.get('historical_retired_count', 0)}`",
        "- Serving-active records: `"
        + str(stats.get("serving_active_count", stats.get("pdf_count", 0)))
        + "`",
        "- Weak-title auto-merge: `false`",
        "",
        "## Acquisition batch checkpoint",
        "",
    ]
    if not batch.get("enabled"):
        lines.append("Acquisition batch checking was not enabled.")
    else:
        lines.extend(
            [
                f"- Census PDFs across batch roots: `{batch.get('pdf_count')}`",
                f"- Batch roots: `{', '.join(batch.get('roots') or [])}`",
                f"- Current checkpoint `{batch.get('expected_current_count')}` matches: "
                f"`{str(bool(batch.get('current_count_matches'))).lower()}`",
                f"- Target checkpoint `{batch.get('expected_target_count')}` reached: "
                f"`{str(bool(batch.get('target_reached'))).lower()}`",
                f"- Remaining to target: `{batch.get('remaining_to_target')}`",
                f"- Duplicate SHA-256 rows: `{batch.get('duplicate_sha256_count')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Dispositions",
            "",
            "| Disposition | Count |",
            "|---|---:|",
        ]
    )
    for name, count in (stats.get("by_disposition") or {}).items():
        lines.append(f"| `{name}` | {count} |")
    lines.extend(
        [
            "",
            "## Census classification",
            "",
            "| Classification | Count |",
            "|---|---:|",
        ]
    )
    for name, count in (stats.get("by_classification") or {}).items():
        lines.append(f"| `{name}` | {count} |")
    lines.extend(
        [
            "",
            "## Unresolved flags",
            "",
            "| Flag | Count |",
            "|---|---:|",
        ]
    )
    unresolved = stats.get("unresolved_flags") or {}
    if unresolved:
        for name, count in unresolved.items():
            lines.append(f"| `{name}` | {count} |")
    else:
        lines.append("| _none_ | 0 |")
    lines.extend(
        [
            "",
            "## Paper records",
            "",
            "| PDF | Classification | Match | Disposition | HTML | Code | Hub relation | Flags |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for record in report.get("records") or []:
        path = str(record.get("absolute_path") or record.get("relative_path") or "").replace(
            "|", "\\|"
        )
        match = (record.get("canonical_match") or {}).get("status")
        html = (record.get("html") or {}).get("status")
        code = (record.get("code") or {}).get("status")
        relation = (record.get("hub_relation") or {}).get("implemented_by_status")
        flags = ", ".join(record.get("unresolved_flags") or []) or "—"
        lines.append(
            f"| `{path}` | `{record.get('classification')}` | `{match}` | "
            f"`{record.get('disposition')}` | `{html}` | "
            f"`{code}` | `{relation}` | {flags} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "DOI, arXiv, PDF SHA-256, exact declared paths, Hub provenance, and explicit",
            "`same_as`/`version_of` declarations may select a canonical candidate. Title",
            "similarity is review evidence only and never performs an automatic merge.",
            "A confirmed Hub code relation requires an `implemented_by` edge in an supplied",
            "Hub catalog; a repository mentioned only by a source manifest remains unverified.",
            "When `--retirement-status` is supplied, retired source representations are",
            "excluded from serving closure aggregation but retained as historical rows.",
            "",
        ]
    )
    return "\n".join(lines)


def _normalize_prefix(path: Path) -> Path:
    return path.with_suffix("") if path.suffix.casefold() in {".json", ".csv", ".md"} else path


def _atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(content, encoding=encoding, newline="")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_reports(output_prefix: Path, report: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    prefix = _normalize_prefix(output_prefix)
    json_path = Path(f"{prefix}.json")
    csv_path = Path(f"{prefix}.csv")
    markdown_path = Path(f"{prefix}.md")
    _atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = csv_path.with_name(f".{csv_path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(_csv_rows(report.get("records") or []))
        os.replace(temporary, csv_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    _atomic_write(markdown_path, _markdown(report))
    return json_path, csv_path, markdown_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit PDF/HTML/code/Hub closure without mutating any source or Hub state."
    )
    parser.add_argument("--census", type=Path, required=True, help="paper-census/v1 JSON")
    parser.add_argument("--sources-yaml", type=Path, required=True)
    parser.add_argument("--hub-root", type=Path)
    parser.add_argument("--manifest", action="append", type=Path, default=[])
    parser.add_argument("--catalog", action="append", type=Path, default=[])
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--batch-root", type=Path)
    parser.add_argument(
        "--batch-additional-root",
        action="append",
        type=Path,
        default=[],
        help="additional core-domain recovery root counted in the acquisition batch",
    )
    parser.add_argument("--batch-expected-current", type=int, default=336)
    parser.add_argument("--batch-expected-target", type=int, default=376)
    parser.add_argument(
        "--require-batch-count",
        choices=("none", "current", "target"),
        default="none",
    )
    parser.add_argument("--no-code-allowlist", type=Path)
    parser.add_argument(
        "--retirement-status",
        type=Path,
        help="optional source lifecycle status JSON; retired source evidence is historical-only",
    )
    parser.add_argument("--weak-title-threshold", type=float, default=0.94)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return 3 when any PDF remains unresolved or evidence is incomplete",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        census = load_census(args.census)
        source_evidence, runtimes, source_errors = load_sources_evidence(
            args.sources_yaml,
            hub_root=args.hub_root,
        )
        extra_paths = [*args.manifest, *args.catalog]
        extra_evidence, documents, document_errors = load_document_evidence(
            extra_paths,
            runtimes=runtimes,
        )
        allowlist = args.no_code_allowlist
        if allowlist is None:
            candidate = args.sources_yaml.resolve().parent / "check-unit-actions-allowlist.yaml"
            allowlist = candidate if candidate.is_file() else None
        report = audit_closure(
            census,
            [*source_evidence, *extra_evidence],
            core_root=args.core_root,
            batch_root=args.batch_root,
            batch_additional_roots=args.batch_additional_root,
            batch_expected_current=args.batch_expected_current,
            batch_expected_target=args.batch_expected_target,
            no_code_titles=load_no_code_titles(allowlist),
            weak_title_threshold=args.weak_title_threshold,
            source_errors=[*source_errors, *document_errors],
            input_documents=documents,
            retirement_status_path=args.retirement_status,
        )
        write_reports(args.output_prefix, report)
        batch = report["batch_check"]
        stats = report["stats"]
        print(
            f"pdf={stats['pdf_count']} closed={stats['closed_count']} "
            f"action_required={stats['action_required_count']} "
            f"batch={batch['pdf_count']} status={report['status']}"
        )
        print(f"reports={_normalize_prefix(args.output_prefix)}.[json|csv|md]")
        if args.require_batch_count == "current" and not batch["current_count_matches"]:
            return 3
        if args.require_batch_count == "target" and not batch["target_reached"]:
            return 3
        if args.strict and (
            stats["action_required_count"] > 0 or report["status"] == "incomplete_evidence"
        ):
            return 3
        return 0
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as error:
        parser.exit(2, f"paper-closure: {error}\n")
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
