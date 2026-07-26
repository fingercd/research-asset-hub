from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from packages.adapters import (
    AdapterError,
    CanonicalSnapshotAdapter,
    CanonicalSnapshotDocument,
    canonical_snapshot_json_schema,
)
from packages.adapters import canonical_snapshot as snapshot_adapter_module
from packages.domain.schemas import SourceConfig
from packages.ingestion.canonical_snapshot import (
    build_canonical_snapshot,
    write_canonical_snapshot,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _vln_source(root: Path) -> None:
    paper_id = "paper-1"
    _write_json(
        root / "library/catalog/papers.v1.json",
        {
            "papers": [
                {
                    "paper_id": paper_id,
                    "id": paper_id,
                    "title": "A Navigation Paper",
                    "authors": ["A. Researcher"],
                    "year": 2026,
                    "venue": "TestConf",
                    "arxiv_id": "2601.00001",
                    "landing_url": "https://example.org/paper",
                    "pdf_urls": ["https://example.org/paper.pdf"],
                    "official_repo": "https://github.com/example/navigation",
                    "summary": "摘要",
                    "problem": "问题",
                    "topics": ["navigation"],
                }
            ]
        },
    )
    _write_json(
        root / "library/catalog/repositories.v1.json",
        {
            "items": [
                {
                    "paper_id": paper_id,
                    "url": "https://github.com/example/navigation",
                    "status": "downloaded",
                    "commit": "a" * 40,
                    "license": "MIT",
                    "core_files": ["README.md"],
                }
            ]
        },
    )
    _write_json(
        root / "library/catalog/analysis.v1.json",
        {
            "items": [
                {
                    "paper_id": paper_id,
                    "analysis": "library/analysis/paper-1.md",
                }
            ]
        },
    )
    _write_json(
        root / "library/catalog/code_guides.v1.json",
        {
            "items": [
                {
                    "paper_id": paper_id,
                    "guide": "library/code/guides/paper-1/README.md",
                    "commit": "a" * 40,
                }
            ]
        },
    )
    _write_json(
        root / "outputs/manifest.json",
        {
            "papers": [
                {
                    "paper_id": paper_id,
                    "paper": {
                        "status": "valid",
                        "path": "library/papers/paper-1/paper.pdf",
                        "sha256": None,
                        "bytes": 9,
                    },
                    "analysis_html": {
                        "status": "valid",
                        "path": "library/site/papers/paper-1.html",
                    },
                    "analysis": "library/analysis/paper-1.md",
                    "code_guide": "library/code/guides/paper-1/README.md",
                    "repository": {
                        "status": "valid",
                        "path": "library/code/upstream/paper-1",
                        "url": "https://github.com/example/navigation",
                        "commit": "a" * 40,
                        "license": "MIT",
                        "core_files": ["README.md"],
                    },
                }
            ]
        },
    )
    files = {
        "library/papers/paper-1/paper.pdf": b"%PDF-1.4\n",
        "library/site/papers/paper-1.html": b"<h1>Paper</h1>\n",
        "library/analysis/paper-1.md": b"# Analysis\n",
        "library/code/guides/paper-1/README.md": b"# Guide\n",
        "library/code/upstream/paper-1/README.md": b"# Code\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    # This malformed file proves the builder never recursively scans work/.
    (root / "work").mkdir()
    (root / "work/ignored.json").write_text("{not json", encoding="utf-8")


def test_vln_builder_is_deterministic_and_uses_only_explicit_inputs(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _vln_source(source_root)

    first = build_canonical_snapshot(
        profile="vln",
        source_root=source_root,
        source_id="vln-test",
        domain_slug="vln",
    )
    second = build_canonical_snapshot(
        profile="vln",
        source_root=source_root,
        source_id="vln-test",
        domain_slug="vln",
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.items) == 1
    assert len(first.items[0].representations) == 5
    assert len(first.items[0].relations) == 1
    repository = next(
        item
        for item in first.items[0].representations
        if item.representation_type.value == "repository"
    )
    assert repository.entrypoint == "README.md"
    assert repository.revision == "a" * 40
    assert repository.materialization_kind.value == "bundle"
    assert repository.mounts[0].path_ref == "library/code/upstream/paper-1"
    assert first.items[0].relations[0].confirmed is True
    assert (
        first.items[0].field_pointers["title"].json_pointer
        == "/papers/0/title"
    )


def test_canonical_adapter_keeps_sidecar_and_source_roots_separate(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "readonly-source"
    intake = project_root / "config/intake/vln-test/snapshot.json"
    _vln_source(source_root)
    snapshot = build_canonical_snapshot(
        profile="vln",
        source_root=source_root,
        source_id="vln-test",
        domain_slug="vln",
    )
    write_canonical_snapshot(
        snapshot,
        source_root=source_root,
        output_path=intake,
    )
    monkeypatch.setattr(snapshot_adapter_module, "PROJECT_ROOT", project_root)
    config = SourceConfig(
        id="vln-test",
        label="VLN",
        adapter="canonical_snapshot",
        root=str(source_root),
        manifest=str(intake),
        domains=["vln"],
    )

    adapter = CanonicalSnapshotAdapter(config)
    record = adapter.load()[0]

    assert record.source_path == "config/intake/vln-test/snapshot.json"
    assert record.representations[0].path_ref == "library/papers/paper-1/paper.pdf"
    assert record.source_fields["field_pointers"]["title"]["input_path"] == (
        "library/catalog/papers.v1.json"
    )
    mismatched = CanonicalSnapshotAdapter(
        SourceConfig(
            id="vln-test",
            label="VLN",
            adapter="canonical_snapshot",
            root=str(source_root),
            manifest=str(intake),
            domains=["vln", "extra-domain"],
        )
    )
    with pytest.raises(AdapterError, match="domains differ"):
        mismatched.load()


def test_canonical_adapter_rejects_uncontrolled_external_manifest(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    arbitrary = tmp_path / "arbitrary/snapshot.json"
    _vln_source(source_root)
    snapshot = build_canonical_snapshot(
        profile="vln",
        source_root=source_root,
        source_id="vln-test",
        domain_slug="vln",
    )
    write_canonical_snapshot(
        snapshot,
        source_root=source_root,
        output_path=arbitrary,
    )
    monkeypatch.setattr(snapshot_adapter_module, "PROJECT_ROOT", project_root)
    adapter = CanonicalSnapshotAdapter(
        SourceConfig(
            id="vln-test",
            label="VLN",
            adapter="canonical_snapshot",
            root=str(source_root),
            manifest=str(arbitrary),
            domains=["vln"],
        )
    )

    with pytest.raises(AdapterError, match="config/intake/vln-test"):
        adapter.load()


def test_canonical_adapter_rejects_changed_frozen_input(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    intake = project_root / "config/intake/vln-test/manifest.json"
    _vln_source(source_root)
    snapshot = build_canonical_snapshot(
        profile="vln",
        source_root=source_root,
        source_id="vln-test",
        domain_slug="vln",
    )
    write_canonical_snapshot(
        snapshot,
        source_root=source_root,
        output_path=intake,
    )
    monkeypatch.setattr(snapshot_adapter_module, "PROJECT_ROOT", project_root)
    adapter = CanonicalSnapshotAdapter(
        SourceConfig(
            id="vln-test",
            label="VLN",
            adapter="canonical_snapshot",
            root=str(source_root),
            manifest=str(intake),
            domains=["vln"],
        )
    )
    adapter.load()
    catalog = source_root / "library/catalog/papers.v1.json"
    catalog.write_text(
        catalog.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(AdapterError, match="differs from its frozen digest"):
        adapter.load(refresh=True)


def test_snapshot_supports_multiple_collection_domains_without_fanout(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _vln_source(source_root)
    snapshot = build_canonical_snapshot(
        profile="vln",
        source_root=source_root,
        source_id="portal-test",
        domain_slug="collection-a",
    )
    payload = snapshot.model_dump(mode="json")
    payload["domain_slugs"] = ["collection-a", "collection-b"]
    payload["items"][0]["asset"]["domain_slugs"] = ["collection-b"]

    validated = CanonicalSnapshotDocument.model_validate(payload)

    assert validated.domain_slugs == ["collection-a", "collection-b"]
    assert validated.items[0].asset.domain_slugs == ["collection-b"]
    payload["items"][0]["asset"]["domain_slugs"] = ["undeclared"]
    with pytest.raises(ValueError, match="undeclared domains"):
        CanonicalSnapshotDocument.model_validate(payload)


def test_canonical_snapshot_schema_is_standalone_2020_12() -> None:
    schema = canonical_snapshot_json_schema()
    checked_in = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/schemas/canonical-source-snapshot.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["domain_slugs"]["type"] == "array"
    assert checked_in == schema
