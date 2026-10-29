from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from packages.adapters import (
    DEFAULT_REGISTRY,
    AdapterError,
    AdapterErrorCode,
    AdapterRegistry,
    BaseAdapter,
    GenericManifestAdapter,
)
from packages.domain.enums import (
    MaterializationKind,
    RepresentationRole,
    RepresentationType,
)
from packages.domain.schemas import NormalizedRecord, SourceConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def fixture_config(kind: str) -> SourceConfig:
    common: dict[str, Any] = {
        "id": kind,
        "label": kind.replace("_", " ").title(),
        "adapter": kind,
        "root": str(FIXTURES / kind),
        "domain": kind.replace("_", "-"),
        "read_only": True,
    }
    if kind == "agent":
        common |= {
            "manifest": "manifest.json",
            "asset_roots": {
                "pdf": "papers",
                "repository": "code/original",
                "annotation": "code/annotated",
                "html": "site",
            },
        }
    elif kind == "infra":
        common |= {
            "manifest": "manifest.json",
            "asset_roots": {
                "pdf": "pdfs",
                "repository": "code/repos",
                "annotation": "code/papers",
                "html": ".",
            },
        }
    elif kind == "world_model":
        common |= {
            "manifest": "papers.json",
            "auxiliary": {
                "repositories": "repositories.json",
                "paper_download_status": "paper_download_status.json",
                "repository_download_status": "repository_download_status.json",
                "annotation_status": "annotation_status.json",
            },
            "asset_roots": {
                "pdf": "papers",
                "repository": "code/upstream",
                "annotation": "code/annotated",
                "html": "site",
            },
        }
    elif kind == "reinforcement_learning":
        common |= {
            "manifest": "papers.json",
            "auxiliary": {
                "repositories": "repositories.json",
                "code_map": "code_map.json",
                "analyses": "analyses.json",
            },
            "asset_roots": {
                "pdf": "pdfs",
                "repository": "code/repos",
                "annotation": "code/annotations",
                "html": ".",
            },
        }
    elif kind == "fine_tuning":
        common |= {
            "manifest": "manifest.json",
            "asset_roots": {
                "pdf": "papers",
                "repository": "code",
                "annotation": "code",
                "html": "site",
            },
        }
    elif kind == "generation":
        common |= {
            "manifest": "manifest.json",
            "auxiliary": {
                "analyses": "analyses.json",
                "code_map": "code_map.json",
            },
            "asset_roots": {
                "pdf": "papers",
                "repository": "code",
                "annotation": "code",
                "html": "site",
            },
        }
    return SourceConfig.model_validate(common)


@pytest.mark.parametrize(
    "kind",
    [
        "agent",
        "infra",
        "world_model",
        "reinforcement_learning",
        "fine_tuning",
        "generation",
    ],
)
def test_builtin_fixture_satisfies_scan_normalize_validate_preview(kind: str) -> None:
    config = fixture_config(kind)
    adapter = DEFAULT_REGISTRY.create(kind, config, base_dir=PROJECT_ROOT)

    scanned = adapter.scan()
    assert len(scanned) == 1
    adapter.prepare(scanned)
    normalized = adapter.validate(adapter.normalize(scanned[0]))
    assert isinstance(normalized, NormalizedRecord)
    assert normalized.source_id == kind
    assert normalized.source_record_id
    assert normalized.source_path
    assert normalized.source_fields
    assert isinstance(normalized.raw, dict)
    assert isinstance(normalized.warnings, list)

    preview = adapter.preview(limit=1)
    assert preview.total_records == 1
    assert preview.outcomes == {"ready": 1}
    assert preview.sample_records[0].source_record_id == normalized.source_record_id


def test_repeated_fixture_output_is_stable() -> None:
    adapter = DEFAULT_REGISTRY.create(
        "agent",
        fixture_config("agent"),
        base_dir=PROJECT_ROOT,
    )
    first = [record.model_dump(mode="json") for record in adapter.load()]
    second = [record.model_dump(mode="json") for record in adapter.load(refresh=True)]
    assert first == second


@pytest.mark.parametrize(
    ("document", "selector"),
    [
        ([{"id": "p1", "title": "Array Root"}], None),
        ({"papers": [{"id": "p1", "title": "Papers Root"}]}, None),
        ({"items": [{"id": "p1", "title": "Items Root"}]}, None),
        ({"payload": {"records": [{"id": "p1", "title": "Selected Root"}]}}, "payload.records"),
    ],
)
def test_generic_adapter_supports_common_root_shapes(
    tmp_path: Path,
    document: Any,
    selector: str | None,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    options: dict[str, Any] = {
        "field_mapping": {
            "source_record_id": "id",
            "title": "title",
        }
    }
    if selector:
        options["record_selector"] = selector
    config = SourceConfig(
        id="generic",
        label="Generic",
        adapter="generic_manifest",
        root=str(tmp_path),
        manifest="manifest.json",
        domain="new-domain",
        options=options,
    )
    records = GenericManifestAdapter(config).load()
    assert len(records) == 1
    assert records[0].source_record_id == "p1"
    assert records[0].asset.domain_slugs == ["new-domain"]


def test_generic_adapter_preserves_structured_asset_attributes(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "paper-1",
                        "title": "Release audit",
                        "attributes": {
                            "first_public_date": "2026-09-03",
                            "release_status": {
                                "code_open": "official_open",
                                "code_local": "absent",
                                "checkpoint_open": "announced",
                            },
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = SourceConfig(
        id="release-audit",
        label="Release audit",
        adapter="generic_manifest",
        root=str(tmp_path),
        manifest="manifest.json",
        domain="vision-language-navigation",
    )

    record = GenericManifestAdapter(config).load()[0]

    assert record.asset.attributes["first_public_date"] == "2026-09-03"
    assert record.asset.attributes["release_status"] == {
        "code_open": "official_open",
        "code_local": "absent",
        "checkpoint_open": "announced",
    }
    assert record.asset.attributes["generic_mapping"]["attributes"] == "attributes"


def test_representation_helper_preserves_bundle_runtime_contract(tmp_path: Path) -> None:
    (tmp_path / "repo/docs").mkdir(parents=True)
    (tmp_path / "repo/README.md").write_text("# Repo\n", encoding="utf-8")
    config = SourceConfig(
        id="generic",
        label="Generic",
        adapter="generic_manifest",
        root=str(tmp_path),
        manifest="manifest.json",
    )
    adapter = GenericManifestAdapter(config)

    candidate = adapter.representation(
        RepresentationType.REPOSITORY,
        warnings=[],
        role=RepresentationRole.OFFICIAL,
        raw_path="repo",
        entrypoint="README.md",
        revision="a" * 40,
        tree_digest="b" * 64,
        materialization_kind=MaterializationKind.BUNDLE,
        metadata_value={"license": "MIT"},
        mounts=[
            {
                "mount_path": "/docs",
                "source_id": "generic",
                "root_key": "source",
                "path_ref": "repo/docs",
                "priority": 10,
            }
        ],
    )

    assert candidate is not None
    assert candidate.entrypoint == "README.md"
    assert candidate.revision == "a" * 40
    assert candidate.tree_digest == "b" * 64
    assert candidate.materialization_kind == MaterializationKind.BUNDLE
    assert candidate.metadata == {"license": "MIT"}
    assert candidate.mounts[0].mount_path == "/docs"


def test_generic_representation_without_sha_field_does_not_hash_raw_record(
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.html").write_text("<h1>Guide</h1>", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "guide-1",
                        "title": "Guide",
                        "guide_path": "guide.html",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = SourceConfig(
        id="generic",
        label="Generic",
        adapter="generic_manifest",
        root=str(tmp_path),
        manifest="manifest.json",
        options={
            "record_selector": "items",
            "representations": [
                {
                    "type": "html",
                    "role": "guide",
                    "path_field": "guide_path",
                }
            ],
        },
    )

    records = GenericManifestAdapter(config).load()

    assert len(records[0].representations) == 1
    assert records[0].representations[0].sha256 is None


def test_documented_knowledge_unit_templates_match_generic_adapter_contract() -> None:
    template_root = PROJECT_ROOT / "docs" / "templates"
    source_document = yaml.safe_load(
        (template_root / "knowledge-source.example.yaml").read_text(encoding="utf-8")
    )
    source = dict(source_document["sources"][0])
    source["root"] = str(template_root)
    source["manifest"] = "knowledge-manifest.example.json"

    records = GenericManifestAdapter(SourceConfig.model_validate(source)).load()

    assert len(records) == 1
    assert records[0].source_record_id == "example-paper-2026"
    assert {
        str(representation.representation_type) for representation in records[0].representations
    } == {"pdf", "repository", "html", "markdown"}
    assert all(str(representation.role) != "guide" for representation in records[0].representations)
    assert {
        str(representation.role)
        for representation in records[0].representations
        if str(representation.representation_type) == "html"
    } == {"analysis"}
    assert not [warning for warning in records[0].warnings if str(warning.severity) == "error"]


def test_path_traversal_has_structured_error(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    adapter = GenericManifestAdapter(
        SourceConfig(
            id="generic",
            label="Generic",
            adapter="generic_manifest",
            root=str(tmp_path),
            manifest="manifest.json",
        )
    )
    with pytest.raises(AdapterError) as captured:
        adapter.path_ref("../../outside.pdf")
    assert captured.value.code == AdapterErrorCode.PATH_OUTSIDE_ROOT


def test_windows_manifest_path_maps_safely_inside_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", "D:\\Research")
    monkeypatch.setenv("HUB_CONTAINER_WORKSPACE_ROOT", "/workspace")
    adapter = GenericManifestAdapter(
        SourceConfig(
            id="generic",
            label="Generic",
            adapter="generic_manifest",
            root="/workspace/domain",
            manifest="manifest.json",
            asset_roots={"repository": "code"},
        )
    )
    assert (
        adapter.path_ref(
            "D:\\Research\\domain\\code\\owner--repo",
            root_key="repository",
        )
        == "owner--repo"
    )


def test_invalid_json_has_structured_error(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{invalid", encoding="utf-8")
    adapter = GenericManifestAdapter(
        SourceConfig(
            id="generic",
            label="Generic",
            adapter="generic_manifest",
            root=str(tmp_path),
            manifest="manifest.json",
        )
    )
    with pytest.raises(AdapterError) as captured:
        adapter.scan()
    assert captured.value.code == AdapterErrorCode.INVALID_JSON


def test_duplicate_source_record_id_is_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"id": "duplicate", "title": "First"},
                {"id": "duplicate", "title": "Second"},
            ]
        ),
        encoding="utf-8",
    )
    adapter = GenericManifestAdapter(
        SourceConfig(
            id="generic",
            label="Generic",
            adapter="generic_manifest",
            root=str(tmp_path),
            manifest="manifest.json",
        )
    )
    with pytest.raises(AdapterError) as captured:
        adapter.load()
    assert captured.value.code == AdapterErrorCode.DUPLICATE_SOURCE_RECORD_ID


def test_registry_accepts_future_adapter_class() -> None:
    registry = AdapterRegistry()
    registry.register("generic_manifest", GenericManifestAdapter)
    assert registry.get("generic_manifest") is GenericManifestAdapter
    assert registry.available() == ("generic_manifest",)
    assert issubclass(registry.get("generic_manifest"), BaseAdapter)


def test_rl_stale_absolute_repository_path_is_safely_relocated(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    repository_root = source_root / "code" / "repos"
    current_repository = repository_root / "example__implementation"
    current_repository.mkdir(parents=True)
    (source_root / "papers.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "title": "Paper",
                    "year": "2024",
                    "source_url": "https://example.org/paper",
                    "paper_page": "papers/001.html",
                    "repositories": [
                        {
                            "url": "https://github.com/example/implementation",
                            "relation": "official_implementation",
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    (source_root / "analyses.json").write_text("[]", encoding="utf-8")
    (source_root / "code_map.json").write_text(
        json.dumps(
            {
                "papers": [
                    {
                        "id": 1,
                        "repositories": [
                            {
                                "url": "https://github.com/example/implementation",
                                "relation": "official_implementation",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (source_root / "repositories.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://github.com/example/implementation",
                    "canonical_url": "https://github.com/example/implementation",
                    "local_path": ("C:\\stale\\untrusted\\parents\\example__implementation"),
                    "status": "cloned",
                }
            ]
        ),
        encoding="utf-8",
    )
    config = SourceConfig(
        id="rl-relocation",
        label="RL relocation",
        adapter="reinforcement_learning",
        root=str(source_root),
        manifest="papers.json",
        domain="reinforcement-learning",
        auxiliary={
            "repositories": "repositories.json",
            "code_map": "code_map.json",
            "analyses": "analyses.json",
        },
        asset_roots={
            "pdf": "pdfs",
            "repository": "code/repos",
            "annotation": "code/annotations",
            "html": ".",
        },
    )
    record = DEFAULT_REGISTRY.create(
        "reinforcement_learning",
        config,
    ).load()[0]
    repository_representations = [
        candidate
        for candidate in record.representations
        if candidate.representation_type.value == "repository"
    ]
    assert len(repository_representations) == 1
    assert repository_representations[0].path_ref == "example__implementation"
    warning_codes = [warning.code for warning in record.warnings]
    assert "stale_repository_path_relocated" in warning_codes
    assert "path_outside_root" not in warning_codes


def test_all_configured_source_record_counts_when_collections_are_present(
    local_research_assets: None,
) -> None:
    sources_file = PROJECT_ROOT / "config" / "sources.yaml"
    document = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
    configured = [SourceConfig.model_validate(item) for item in document["sources"]]
    if not all((PROJECT_ROOT / config.root_path).resolve().is_dir() for config in configured):
        pytest.skip("The configured local collections are not present beside the project")

    actual: dict[str, int] = {}
    expected: dict[str, int] = {}
    reinforcement_learning_records: list[NormalizedRecord] = []
    for config in configured:
        assert config.expected_records is not None
        expected[config.source_id] = config.expected_records
        adapter = DEFAULT_REGISTRY.create(
            config.adapter,
            config,
            base_dir=PROJECT_ROOT,
        )
        records = adapter.load()
        actual[config.source_id] = len(records)
        if config.source_id == "reinforcement_learning":
            reinforcement_learning_records = records
        assert all(record.source_record_id for record in records)
        assert all(record.source_path for record in records)
        assert all(isinstance(record.warnings, list) for record in records)

    assert actual == expected
    rl_warning_codes = [
        warning.code for record in reinforcement_learning_records for warning in record.warnings
    ]
    rl_repository_representations = [
        candidate
        for record in reinforcement_learning_records
        for candidate in record.representations
        if candidate.representation_type.value == "repository"
    ]
    assert "path_outside_root" not in rl_warning_codes
    assert len(rl_repository_representations) == 72
