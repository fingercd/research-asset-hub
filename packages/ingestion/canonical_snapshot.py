"""Deterministically join rich source catalogs into the canonical sidecar contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from pydantic import AnyUrl

from packages.adapters.base import stable_uuid7
from packages.adapters.canonical_snapshot import (
    CanonicalFieldPointer,
    CanonicalSnapshotDocument,
    CanonicalSnapshotInput,
    CanonicalSnapshotItem,
)
from packages.domain.enums import (
    MaterializationKind,
    RelationType,
    RepresentationAvailability,
    RepresentationRole,
    RepresentationType,
)
from packages.domain.schemas import (
    AssetReference,
    ExternalIdentifier,
    NormalizedAsset,
    RelationCandidate,
    RelationEvidence,
    RepresentationCandidate,
    RepresentationMountCandidate,
)

CanonicalProfile = Literal["vln", "game_rl", "wsvad"]
_VALID_LOCAL_STATUSES = {"downloaded", "valid"}


@dataclass(frozen=True, slots=True)
class _ProfileSpec:
    papers_path: str
    repositories_path: str
    manifest_path: str
    analysis_path: str | None = None
    code_guides_path: str | None = None

    @property
    def input_paths(self) -> tuple[str, ...]:
        return tuple(
            path
            for path in (
                self.papers_path,
                self.repositories_path,
                self.analysis_path,
                self.code_guides_path,
                self.manifest_path,
            )
            if path is not None
        )


_PROFILE_SPECS: dict[CanonicalProfile, _ProfileSpec] = {
    "vln": _ProfileSpec(
        papers_path="library/catalog/papers.v1.json",
        repositories_path="library/catalog/repositories.v1.json",
        analysis_path="library/catalog/analysis.v1.json",
        code_guides_path="library/catalog/code_guides.v1.json",
        manifest_path="outputs/manifest.json",
    ),
    "game_rl": _ProfileSpec(
        papers_path="library/catalog/papers.json",
        repositories_path="library/catalog/repositories.json",
        manifest_path="outputs/manifest.json",
    ),
    "wsvad": _ProfileSpec(
        papers_path="library/catalog/papers.v1.json",
        repositories_path="library/catalog/repositories.v1.json",
        code_guides_path="library/catalog/code_guides.v1.json",
        manifest_path="outputs/manifest.json",
    ),
}


@dataclass(frozen=True, slots=True)
class _LoadedInput:
    contract: CanonicalSnapshotInput
    document: dict[str, Any]


def _load_input(source_root: Path, relative_path: str) -> _LoadedInput:
    path = source_root / relative_path
    if not path.is_file():
        raise ValueError(f"required canonical snapshot input does not exist: {relative_path}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON input {relative_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"canonical snapshot input must be an object: {relative_path}")
    return _LoadedInput(
        contract=CanonicalSnapshotInput(
            path=relative_path,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        ),
        document=cast(dict[str, Any], document),
    )


def _array(document: Mapping[str, Any], key: str, input_path: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{input_path} must contain an array at {key}")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{input_path} {key}[{index}] must be an object")
        result.append(cast(dict[str, Any], item))
    return result


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _urls(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text is None:
            continue
        parsed = urlparse(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        if text not in result:
            result.append(text)
    return result


def _json_values(value: Mapping[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(json.dumps(dict(value), ensure_ascii=False)),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_digest(source_root: Path, path_ref: str, declared: Any = None) -> str | None:
    declared_text = _text(declared)
    if declared_text is not None:
        normalized = declared_text.lower()
        if len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized):
            return normalized
    path = source_root / path_ref
    return _sha256_file(path) if path.is_file() else None


def _pointer(
    loaded: Mapping[str, _LoadedInput],
    input_path: str,
    json_pointer: str,
) -> CanonicalFieldPointer:
    source_input = loaded[input_path].contract
    return CanonicalFieldPointer(
        input_path=input_path,
        input_sha256=source_input.sha256,
        json_pointer=json_pointer,
    )


def _derived_pointer(output_index: int, field_name: str) -> CanonicalFieldPointer:
    return CanonicalFieldPointer(
        json_pointer=f"/items/{output_index}/asset/{field_name}"
    )


def _identifiers(paper: Mapping[str, Any], urls: Sequence[str]) -> list[ExternalIdentifier]:
    identifiers: list[ExternalIdentifier] = []
    doi = _text(paper.get("doi"))
    arxiv = _text(paper.get("arxiv_id"))
    if doi:
        identifiers.append(ExternalIdentifier(scheme="doi", value=doi))
    if arxiv:
        identifiers.append(ExternalIdentifier(scheme="arxiv", value=arxiv))
    if not identifiers and urls:
        identifiers.append(ExternalIdentifier(scheme="url", value=urls[0], is_identity=False))
    return identifiers


def _paper_identity(profile: CanonicalProfile, paper: Mapping[str, Any]) -> str:
    key = "id" if profile == "game_rl" else "paper_id"
    record_id = _text(paper.get(key))
    if record_id is None:
        raise ValueError(f"{profile} paper is missing {key}")
    return record_id


def _paper_asset(
    *,
    profile: CanonicalProfile,
    paper: Mapping[str, Any],
    source_id: str,
    record_id: str,
    domain_slug: str,
) -> NormalizedAsset:
    analysis_keys: tuple[str, ...]
    attribute_keys: tuple[str, ...]
    if profile == "game_rl":
        summary = _text(paper.get("one_sentence"))
        analysis_keys = (
            "problem",
            "mechanism_steps",
            "game_context",
            "experiments",
            "key_results",
            "contributions",
            "limitations",
            "lineage",
            "reproducibility",
        )
        attribute_keys = (
            "first_public_date",
            "venue_publication_date",
            "publication_status",
            "paper_kind",
            "scope_tier",
            "primary_track",
            "is_survey",
            "is_milestone",
            "is_recent",
            "game_metrics",
            "confidence",
            "curation_note",
            "repository_id",
        )
        source_urls = _urls(
            [
                paper.get("landing_url"),
                *_strings(paper.get("pdf_urls")),
                paper.get("code_url"),
                paper.get("third_party_code_url"),
                *_strings(paper.get("evidence_urls")),
            ]
        )
        tags = _strings(paper.get("tags"))
        for value in (paper.get("primary_track"), paper.get("scope_tier")):
            text = _text(value)
            if text and text not in tags:
                tags.append(text)
    else:
        summary = _text(paper.get("summary"))
        analysis_keys = (
            "problem",
            "method",
            "contributions",
            "experiments",
            "limitations",
            "relations",
            "evidence_notes",
            "milestone_reason",
            "formula_family",
        )
        attribute_keys = (
            "first_public_date",
            "publication_status",
            "status",
            "sets",
            "kind",
            "primary_topic",
            "topics",
            "topic_label",
            "module",
            "scope",
            "themes",
            "datasets",
            "metrics",
            "trend_eligible",
            "tasks",
        )
        source_urls = _urls(
            [
                paper.get("landing_url"),
                paper.get("page_url"),
                *_strings(paper.get("pdf_urls")),
                paper.get("official_repo"),
            ]
        )
        tags = _strings(paper.get("topics")) + _strings(paper.get("themes"))
        tags = list(
            dict.fromkeys(
                tags + _strings(paper.get("tasks")) + _strings(paper.get("sets"))
            )
        )

    title = _text(paper.get("title"))
    if title is None:
        raise ValueError(f"{profile}:{record_id} has no title")
    year_value = paper.get("year")
    year = int(year_value) if isinstance(year_value, (int, str)) and str(year_value) else None
    analysis = {
        key: _json_values({"value": paper[key]})["value"]
        for key in analysis_keys
        if paper.get(key) not in (None, "", [], {})
    }
    attributes = {
        key: _json_values({"value": paper[key]})["value"]
        for key in attribute_keys
        if paper.get(key) not in (None, "", [], {})
    }
    attributes["canonical_profile"] = profile
    return NormalizedAsset(
        asset_id=stable_uuid7(source_id, record_id),
        asset_type="paper",
        title=title,
        title_zh=_text(paper.get("title_zh")),
        authors=_strings(paper.get("authors")),
        year=year,
        venue=_text(paper.get("venue")),
        abstract=_text(paper.get("abstract")),
        summary=summary,
        analysis=analysis,
        external_identifiers=_identifiers(paper, source_urls),
        source_urls=[AnyUrl(url) for url in source_urls],
        domain_slugs=[domain_slug],
        tags=tags,
        attributes=attributes,
    )


def _field_pointers(
    *,
    loaded: Mapping[str, _LoadedInput],
    papers_path: str,
    papers_key: str,
    paper_index: int,
    output_index: int,
    asset: NormalizedAsset,
    profile: CanonicalProfile,
) -> dict[str, CanonicalFieldPointer]:
    prefix = f"/{papers_key}/{paper_index}"
    source_fields = {
        "title": "title",
        "title_zh": "title_zh",
        "authors": "authors",
        "year": "year",
        "venue": "venue",
        "abstract": "abstract",
        "summary": "one_sentence" if profile == "game_rl" else "summary",
    }
    payload = asset.model_dump(mode="json")
    pointers: dict[str, CanonicalFieldPointer] = {}
    for field_name, source_field in source_fields.items():
        if payload.get(field_name) not in (None, "", [], {}):
            pointers[field_name] = _pointer(
                loaded,
                papers_path,
                f"{prefix}/{source_field}",
            )
    for field_name in ("analysis", "source_urls", "tags", "attributes"):
        if payload.get(field_name) not in (None, "", [], {}):
            pointers[field_name] = _pointer(loaded, papers_path, prefix)
    pointers["domain_slugs"] = _derived_pointer(output_index, "domain_slugs")
    return pointers


def _representation(
    *,
    source_root: Path,
    source_id: str,
    representation_type: RepresentationType,
    role: RepresentationRole,
    path_ref: str | None,
    url: str | None = None,
    materialization_kind: MaterializationKind = MaterializationKind.FILE,
    sha256: Any = None,
    size_bytes: Any = None,
    entrypoint: str | None = None,
    revision: Any = None,
    tree_digest: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> RepresentationCandidate | None:
    if path_ref is None and url is None:
        return None
    path = source_root / path_ref if path_ref else None
    available = bool(
        (path is not None and path.exists())
        or (path is None and url is not None)
    )
    declared_size = size_bytes if isinstance(size_bytes, int) and size_bytes >= 0 else None
    digest = (
        _file_digest(source_root, path_ref, sha256)
        if path_ref is not None and materialization_kind == MaterializationKind.FILE
        else None
    )
    digest_text = _text(tree_digest)
    if digest_text is not None and (
        len(digest_text) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in digest_text)
    ):
        digest_text = None
    mounts = (
        [
            RepresentationMountCandidate(
                source_id=source_id,
                root_key="source",
                path_ref=path_ref,
            )
        ]
        if path_ref is not None and materialization_kind == MaterializationKind.BUNDLE
        else []
    )
    return RepresentationCandidate(
        representation_type=representation_type,
        role=role,
        materialization_kind=materialization_kind,
        availability=(
            RepresentationAvailability.AVAILABLE
            if available
            else RepresentationAvailability.MISSING
        ),
        source_id=source_id,
        root_key="source",
        path_ref=path_ref,
        url=AnyUrl(url) if url is not None else None,
        entrypoint=entrypoint,
        revision=_text(revision),
        tree_digest=digest_text,
        sha256=digest,
        size_bytes=declared_size,
        network_required=bool(url and path_ref is None),
        metadata=_json_values(metadata or {}),
        mounts=mounts,
    )


def _repository_relation(
    *,
    source_id: str,
    repository_url: str,
    evidence_path: str,
    evidence_pointer: str,
    evidence_input: CanonicalSnapshotInput,
    metadata: Mapping[str, Any],
) -> RelationCandidate:
    scheme = "github" if urlparse(repository_url).netloc.casefold() == "github.com" else "url"
    return RelationCandidate(
        relation_type=RelationType.IMPLEMENTED_BY,
        object=AssetReference(
            external_identifier=ExternalIdentifier(
                scheme=scheme,
                value=repository_url,
            )
        ),
        evidence=RelationEvidence(
            source=source_id,
            path=evidence_path,
            field=evidence_pointer,
            excerpt=repository_url,
            details={
                "input_sha256": evidence_input.sha256,
                "contract": "confirmed repository binding",
            },
        ),
        confidence=1.0,
        confirmed=True,
        metadata=_json_values(metadata),
    )


def _index_by(
    items: Sequence[dict[str, Any]],
    key: str,
    *,
    label: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    by_id: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for index, item in enumerate(items):
        item_id = _text(item.get(key))
        if item_id is None:
            raise ValueError(f"{label}[{index}] is missing {key}")
        if item_id in by_id:
            raise ValueError(f"{label} contains duplicate {key}: {item_id}")
        by_id[item_id] = item
        positions[item_id] = index
    return by_id, positions


def _validate_exact_join(expected: set[str], actual: set[str], label: str) -> None:
    if expected == actual:
        return
    raise ValueError(
        f"{label} does not join exactly to papers: "
        f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
    )


def build_canonical_snapshot(
    *,
    profile: CanonicalProfile,
    source_root: Path,
    source_id: str,
    domain_slug: str,
) -> CanonicalSnapshotDocument:
    """Build a canonical snapshot from only the profile's explicit input files."""

    root = source_root.resolve()
    spec = _PROFILE_SPECS[profile]
    loaded = {path: _load_input(root, path) for path in spec.input_paths}
    papers_key = "papers"
    papers = _array(loaded[spec.papers_path].document, papers_key, spec.papers_path)
    papers_by_id, paper_positions = _index_by(
        papers,
        "id" if profile == "game_rl" else "paper_id",
        label=spec.papers_path,
    )
    paper_ids = set(papers_by_id)

    manifest_key = "items" if profile == "game_rl" else "papers"
    manifest_items = _array(
        loaded[spec.manifest_path].document,
        manifest_key,
        spec.manifest_path,
    )
    manifest_by_id, manifest_positions = _index_by(
        manifest_items,
        "id" if profile == "game_rl" else "paper_id",
        label=spec.manifest_path,
    )
    _validate_exact_join(paper_ids, set(manifest_by_id), spec.manifest_path)

    analysis_by_id: dict[str, dict[str, Any]] = {}
    if spec.analysis_path is not None:
        analysis_items = _array(
            loaded[spec.analysis_path].document,
            "items",
            spec.analysis_path,
        )
        analysis_by_id, _ = _index_by(
            analysis_items,
            "paper_id",
            label=spec.analysis_path,
        )
        _validate_exact_join(paper_ids, set(analysis_by_id), spec.analysis_path)

    guides_by_id: dict[str, dict[str, Any]] = {}
    if spec.code_guides_path is not None:
        guide_items = _array(
            loaded[spec.code_guides_path].document,
            "items",
            spec.code_guides_path,
        )
        guides_by_id, _ = _index_by(
            guide_items,
            "paper_id",
            label=spec.code_guides_path,
        )
        _validate_exact_join(paper_ids, set(guides_by_id), spec.code_guides_path)

    repositories_document = loaded[spec.repositories_path].document
    repositories_key = "repositories" if profile == "game_rl" else "items"
    repository_items = _array(
        repositories_document,
        repositories_key,
        spec.repositories_path,
    )
    repositories_by_paper: dict[str, dict[str, Any]] = {}
    repository_positions: dict[str, int] = {}
    if profile == "game_rl":
        repositories_by_id, repository_id_positions = _index_by(
            repository_items,
            "id",
            label=spec.repositories_path,
        )
        for repository_id, repository_item in repositories_by_id.items():
            for paper_id in _strings(repository_item.get("paper_ids")):
                if paper_id not in paper_ids:
                    raise ValueError(
                        f"{spec.repositories_path} references unknown paper: {paper_id}"
                    )
                if paper_id in repositories_by_paper:
                    raise ValueError(f"paper has more than one repository binding: {paper_id}")
                repositories_by_paper[paper_id] = repository_item
                repository_positions[paper_id] = repository_id_positions[repository_id]
    else:
        repositories_by_paper, repository_positions = _index_by(
            repository_items,
            "paper_id",
            label=spec.repositories_path,
        )
        _validate_exact_join(paper_ids, set(repositories_by_paper), spec.repositories_path)

    items: list[CanonicalSnapshotItem] = []
    for output_index, record_id in enumerate(sorted(paper_ids)):
        paper = papers_by_id[record_id]
        manifest_item = manifest_by_id[record_id]
        manifest_repo = manifest_item.get("repository")
        repository_manifest = (
            cast(dict[str, Any], manifest_repo)
            if isinstance(manifest_repo, dict)
            else {}
        )
        repository_catalog = repositories_by_paper.get(record_id, {})
        asset = _paper_asset(
            profile=profile,
            paper=paper,
            source_id=source_id,
            record_id=record_id,
            domain_slug=domain_slug,
        )

        representations: list[RepresentationCandidate] = []
        paper_manifest = manifest_item.get("paper") or manifest_item.get("pdf")
        pdf_manifest = (
            cast(dict[str, Any], paper_manifest)
            if isinstance(paper_manifest, dict)
            else {}
        )
        pdf = _representation(
            source_root=root,
            source_id=source_id,
            representation_type=RepresentationType.PDF,
            role=RepresentationRole.ORIGINAL,
            path_ref=_text(pdf_manifest.get("path")),
            sha256=pdf_manifest.get("sha256"),
            size_bytes=pdf_manifest.get("bytes"),
            metadata={
                "status": pdf_manifest.get("status"),
                "pages": pdf_manifest.get("pages"),
                "source_url": pdf_manifest.get("source_url"),
            },
        )
        if pdf is None:
            raise ValueError(f"{profile}:{record_id} has no declared PDF representation")
        representations.append(pdf)

        html_manifest = manifest_item.get("analysis_html")
        html_data = (
            cast(dict[str, Any], html_manifest)
            if isinstance(html_manifest, dict)
            else {}
        )
        html = _representation(
            source_root=root,
            source_id=source_id,
            representation_type=RepresentationType.HTML,
            role=RepresentationRole.ANALYSIS,
            path_ref=_text(html_data.get("path")),
            size_bytes=html_data.get("bytes"),
            metadata={
                "status": html_data.get("status"),
                "links_checked": html_data.get("links_checked"),
                "section_count": html_data.get("section_count"),
            },
        )
        if html is None:
            raise ValueError(f"{profile}:{record_id} has no declared HTML representation")
        representations.append(html)

        analysis_row = analysis_by_id.get(record_id)
        if analysis_row is not None:
            analysis = _representation(
                source_root=root,
                source_id=source_id,
                representation_type=RepresentationType.ANALYSIS,
                role=RepresentationRole.ANALYSIS,
                path_ref=_text(analysis_row.get("analysis")),
                metadata=analysis_row,
            )
            if analysis is None:
                raise ValueError(
                    f"{profile}:{record_id} has no declared analysis representation"
                )
            representations.append(analysis)

        guide_path = (
            _text(guides_by_id.get(record_id, {}).get("guide"))
            or _text(repository_manifest.get("guide"))
            or _text(manifest_item.get("code_guide"))
        )
        guide_metadata = guides_by_id.get(record_id, repository_manifest)
        guide = _representation(
            source_root=root,
            source_id=source_id,
            representation_type=RepresentationType.GUIDE,
            role=RepresentationRole.GUIDE,
            path_ref=guide_path,
            revision=guide_metadata.get("commit"),
            metadata=guide_metadata,
        )
        if guide is None:
            raise ValueError(f"{profile}:{record_id} has no declared code guide")
        representations.append(guide)

        repository_status = _text(repository_manifest.get("status"))
        repository_url = _text(
            repository_manifest.get("url")
            or repository_catalog.get("url")
            or paper.get("official_repo")
            or paper.get("code_url")
        )
        repository_path = _text(repository_manifest.get("path"))
        repository: RepresentationCandidate | None = None
        if repository_status in _VALID_LOCAL_STATUSES and repository_url:
            core_files = _strings(
                repository_manifest.get("core_files")
                or repository_catalog.get("core_files")
            )
            repository_metadata = {
                **_json_values(repository_catalog),
                **_json_values(repository_manifest),
            }
            repository = _representation(
                source_root=root,
                source_id=source_id,
                representation_type=RepresentationType.REPOSITORY,
                role=RepresentationRole.OFFICIAL,
                materialization_kind=MaterializationKind.BUNDLE,
                path_ref=repository_path,
                url=repository_url,
                entrypoint=core_files[0] if core_files else None,
                revision=repository_manifest.get("commit")
                or repository_catalog.get("commit"),
                tree_digest=repository_manifest.get("tree_digest"),
                size_bytes=repository_manifest.get("bytes"),
                metadata=repository_metadata,
            )
            if repository is not None:
                representations.append(repository)

        relations: list[RelationCandidate] = []
        if repository is not None and repository_url is not None:
            repository_position = repository_positions.get(record_id)
            if profile == "game_rl" and repository_position is not None:
                evidence_path = spec.repositories_path
                evidence_pointer = f"/repositories/{repository_position}/url"
            else:
                evidence_path = spec.manifest_path
                evidence_pointer = (
                    f"/{manifest_key}/{manifest_positions[record_id]}/repository/url"
                )
            relations.append(
                _repository_relation(
                    source_id=source_id,
                    repository_url=repository_url,
                    evidence_path=evidence_path,
                    evidence_pointer=evidence_pointer,
                    evidence_input=loaded[evidence_path].contract,
                    metadata={
                        "binding_status": repository_status,
                        "source_type": repository_catalog.get("source_type"),
                        "repo_type": repository_catalog.get("repo_type"),
                        "officiality_evidence": repository_catalog.get(
                            "officiality_evidence"
                        ),
                    },
                )
            )

        source_payload: dict[str, Any] = {
            "catalog_paper": _json_values(paper),
            "manifest_item": _json_values(manifest_item),
        }
        if repository_catalog:
            source_payload["catalog_repository"] = _json_values(repository_catalog)
        if analysis_row is not None:
            source_payload["catalog_analysis"] = _json_values(analysis_row)
        if record_id in guides_by_id:
            source_payload["catalog_code_guide"] = _json_values(guides_by_id[record_id])

        items.append(
            CanonicalSnapshotItem(
                source_record_id=record_id,
                asset=asset,
                field_pointers=_field_pointers(
                    loaded=loaded,
                    papers_path=spec.papers_path,
                    papers_key=papers_key,
                    paper_index=paper_positions[record_id],
                    output_index=output_index,
                    asset=asset,
                    profile=profile,
                ),
                representations=representations,
                relations=relations,
                source_payload=source_payload,
            )
        )

    return CanonicalSnapshotDocument(
        source_id=source_id,
        profile=profile,
        domain_slugs=[domain_slug],
        inputs=[loaded[path].contract for path in sorted(loaded)],
        items=items,
    )


def write_canonical_snapshot(
    snapshot: CanonicalSnapshotDocument,
    *,
    source_root: Path,
    output_path: Path,
) -> None:
    """Write outside the read-only source root using stable JSON serialization."""

    root = source_root.resolve()
    output = output_path.resolve()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("canonical snapshot output must be outside the read-only source root")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
