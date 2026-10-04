from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from packages.managed_sources.service import data_directory, load_managed_sources

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES_CONFIG = PROJECT_ROOT / "config" / "sources.yaml"
DEFAULT_TAXONOMY_CONFIG = PROJECT_ROOT / "config" / "taxonomy.yaml"


@dataclass(frozen=True, slots=True)
class LoadedSource:
    """Resolved source configuration without mutating its YAML representation."""

    source_id: str
    label: str
    domain: str
    adapter: str
    root: Path
    manifest: Path
    expected_records: int | None
    read_only: bool
    raw: dict[str, Any]

    def resolve_source_path(self, value: str | Path) -> Path:
        return (self.root / Path(value)).resolve()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at YAML root: {path}")
    return data


def load_sources(
    path: Path = DEFAULT_SOURCES_CONFIG,
    *,
    data_dir: Path | None = None,
    include_platform: bool = False,
) -> list[LoadedSource]:
    document = load_yaml(path)
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError(f"'sources' must be a list: {path}")
    # Platform repository snapshots have their own immutable ledger and do not
    # implement a formal adapter contract. PDF and domain-package manifests join
    # the formal adapter pipeline. Read-only inspection explicitly includes all
    # registered platform manifests without dispatching them through adapters.
    raw_sources = [
        *raw_sources,
        *[
            item
            for item in load_managed_sources(data_dir or data_directory())
            if include_platform or item.get("adapter") in {"generic_manifest", "domain_package"}
        ],
    ]

    loaded: list[LoadedSource] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise ValueError(f"sources[{index}] must be a mapping")
        source_id = str(item.get("id", "")).strip()
        if not source_id:
            raise ValueError(f"sources[{index}] is missing id")
        if source_id in seen_ids:
            raise ValueError(f"Duplicate source id: {source_id}")
        seen_ids.add(source_id)

        root_value = item.get("root")
        manifest_value = item.get("manifest")
        if not isinstance(root_value, str) or not isinstance(manifest_value, str):
            raise ValueError(f"Source {source_id} requires string root and manifest")

        root = (PROJECT_ROOT / root_value).resolve()
        manifest = (root / manifest_value).resolve()
        loaded.append(
            LoadedSource(
                source_id=source_id,
                label=str(item.get("label") or source_id),
                domain=str(item.get("domain") or source_id),
                adapter=str(item.get("adapter") or "generic_manifest"),
                root=root,
                manifest=manifest,
                expected_records=(
                    int(item["expected_records"])
                    if item.get("expected_records") is not None
                    else None
                ),
                read_only=bool(item.get("read_only", True)),
                raw=item,
            )
        )
    return loaded


def load_domain_slugs(path: Path = DEFAULT_TAXONOMY_CONFIG) -> set[str]:
    document = load_yaml(path)
    raw_domains = document.get("domains")
    if not isinstance(raw_domains, list):
        raise ValueError(f"'domains' must be a list: {path}")
    slugs: set[str] = set()
    for index, item in enumerate(raw_domains):
        if not isinstance(item, dict):
            raise ValueError(f"domains[{index}] must be a mapping")
        slug = str(item.get("id", "")).strip()
        label = str(item.get("label", "")).strip()
        if not slug or not label:
            raise ValueError(f"domains[{index}] requires id and label")
        if slug in slugs:
            raise ValueError(f"Duplicate domain id: {slug}")
        slugs.add(slug)
    return slugs


def unregistered_source_domains(
    sources: list[LoadedSource] | tuple[LoadedSource, ...],
    *,
    known_domain_slugs: set[str] | None = None,
) -> list[str]:
    known = load_domain_slugs() if known_domain_slugs is None else known_domain_slugs
    return sorted({source.domain for source in sources} - known)


def select_sources(
    sources: list[LoadedSource], source_id: str | None, all_sources: bool
) -> list[LoadedSource]:
    if all_sources or not source_id:
        return sources
    matches = [source for source in sources if source.source_id == source_id]
    if not matches:
        available = ", ".join(source.source_id for source in sources)
        raise ValueError(f"Unknown source '{source_id}'. Available: {available}")
    return matches
