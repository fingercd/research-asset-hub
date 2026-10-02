from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.adapters import AdapterError, AdapterErrorCode, GenericManifestAdapter
from packages.cli.config import LoadedSource
from packages.domain.models import Base, RepositoryProfile, Representation
from packages.domain.schemas import SourceConfig
from packages.ingestion import formal

COMMIT = "A" * 40
REPOSITORY_URL = "https://github.com/example/repository"


def test_fixed_representation_metadata_survives_normalization(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        code={"path": "repo", "url": REPOSITORY_URL, "commit": COMMIT,
              "license": "MIT", "status": "downloaded"},
        spec=_repository_spec(metadata={"source_alias": "guide-batch", "unified_guide": True}),
    )
    representation = adapter.load()[0].representations[0]
    assert representation.metadata["source_alias"] == "guide-batch"
    assert representation.metadata["unified_guide"] is True


def _repository_spec(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "repository",
        "role": "official",
        "root_key": "repository",
        "path_field": "representations.code.path",
        "url_field": "representations.code.url",
        "commit_field": "representations.code.commit",
        "license_field": "representations.code.license",
        "status_field": "representations.code.status",
        **overrides,
    }


def _write_manifest(
    tmp_path: Path,
    *,
    code: dict[str, Any] | None,
) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    item: dict[str, Any] = {
        "id": "paper-1",
        "title": "Repository Metadata Paper",
    }
    if code is not None:
        item["representations"] = {"code": code}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    return manifest


def _adapter(
    tmp_path: Path,
    *,
    code: dict[str, Any] | None,
    spec: dict[str, Any] | None = None,
) -> GenericManifestAdapter:
    _write_manifest(tmp_path, code=code)
    config = SourceConfig.model_validate(
        {
            "id": "generic-repository-metadata",
            "label": "Generic repository metadata",
            "adapter": "generic_manifest",
            "root": str(tmp_path),
            "manifest": "manifest.json",
            "domain": "test-domain",
            "asset_roots": {"repository": "."},
            "options": {
                "record_selector": "items",
                "representations": [spec or _repository_spec()],
            },
        }
    )
    return GenericManifestAdapter(config)


def _valid_code(**overrides: Any) -> dict[str, Any]:
    return {
        "path": "repo",
        "url": REPOSITORY_URL,
        "commit": COMMIT,
        "license": "Apache",
        "status": "official_local",
        **overrides,
    }


def test_generic_repository_fields_map_to_candidate_revision_and_metadata(
    tmp_path: Path,
) -> None:
    candidate = _adapter(tmp_path, code=_valid_code()).load()[0].representations[0]

    assert candidate.revision == COMMIT.lower()
    assert candidate.metadata == {
        "mapping_index": 0,
        "license": "Apache",
        "status": "official_local",
    }


def test_generic_revision_field_supports_safe_non_commit_revision(tmp_path: Path) -> None:
    code = _valid_code(revision="refs/tags/v1.2.3")
    code.pop("commit")
    spec = _repository_spec(revision_field="representations.code.revision")
    spec.pop("commit_field")

    candidate = _adapter(tmp_path, code=code, spec=spec).load()[0].representations[0]

    assert candidate.revision == "refs/tags/v1.2.3"


@pytest.mark.parametrize(
    "invalid_revision",
    [
        None,
        "",
        7,
        ["v1"],
        "a" * 256,
        "v/../../x",
        "feature//x",
        "main.lock",
        "main/",
        "<script>",
    ],
)
def test_revision_field_rejects_empty_unsafe_and_non_string_values(
    tmp_path: Path,
    invalid_revision: Any,
) -> None:
    code = _valid_code(revision=invalid_revision)
    code.pop("commit")
    spec = _repository_spec(revision_field="representations.code.revision")
    spec.pop("commit_field")

    with pytest.raises(AdapterError) as captured:
        _adapter(tmp_path, code=code, spec=spec).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD
    assert captured.value.details["field"] == "representations.code.revision"


def test_configured_repository_metadata_does_not_require_code_on_absent_entry(
    tmp_path: Path,
) -> None:
    record = _adapter(tmp_path, code=None).load()[0]

    assert record.representations == []


def test_unconfigured_repository_metadata_preserves_legacy_behavior(tmp_path: Path) -> None:
    spec = _repository_spec()
    for key in ("commit_field", "license_field", "status_field"):
        spec.pop(key)

    candidate = _adapter(tmp_path, code=_valid_code(), spec=spec).load()[
        0
    ].representations[0]

    assert candidate.revision is None
    assert candidate.metadata == {"mapping_index": 0}


@pytest.mark.parametrize(
    "invalid_commit",
    [None, "", "a" * 39, "g" * 40, 123, ["a" * 40]],
)
def test_commit_field_rejects_empty_malformed_and_non_string_values(
    tmp_path: Path,
    invalid_commit: Any,
) -> None:
    with pytest.raises(AdapterError) as captured:
        _adapter(tmp_path, code=_valid_code(commit=invalid_commit)).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD
    assert captured.value.details["field"] == "representations.code.commit"


@pytest.mark.parametrize(
    "invalid_license",
    [None, "", "<script>", "MIT\nGPL", "A" * 128, 42, ["MIT"]],
)
def test_license_field_rejects_empty_unsafe_and_non_string_values(
    tmp_path: Path,
    invalid_license: Any,
) -> None:
    with pytest.raises(AdapterError) as captured:
        _adapter(tmp_path, code=_valid_code(license=invalid_license)).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD
    assert captured.value.details["field"] == "representations.code.license"


@pytest.mark.parametrize(
    "invalid_status",
    [None, "", "official local", "<script>", "a" * 33, 7],
)
def test_status_field_rejects_empty_unsafe_and_non_string_values(
    tmp_path: Path,
    invalid_status: Any,
) -> None:
    with pytest.raises(AdapterError) as captured:
        _adapter(tmp_path, code=_valid_code(status=invalid_status)).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD
    assert captured.value.details["field"] == "representations.code.status"


@pytest.mark.parametrize("selector", [None, "", "code..commit", "../commit", 7, []])
def test_repository_metadata_selector_must_be_a_safe_non_empty_string(
    tmp_path: Path,
    selector: Any,
) -> None:
    spec = _repository_spec(commit_field=selector)

    with pytest.raises(AdapterError) as captured:
        _adapter(tmp_path, code=_valid_code(), spec=spec).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD
    assert captured.value.details["option"] == "commit_field"


def test_commit_and_generic_revision_selectors_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    spec = _repository_spec(revision_field="representations.code.revision")

    with pytest.raises(AdapterError, match="cannot configure both") as captured:
        _adapter(tmp_path, code=_valid_code(revision="v1"), spec=spec).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD


def test_repository_profile_fields_are_rejected_on_non_repository_mapping(
    tmp_path: Path,
) -> None:
    spec = _repository_spec(type="pdf", root_key="repository")

    with pytest.raises(AdapterError, match="valid only for repository") as captured:
        _adapter(tmp_path, code=_valid_code(), spec=spec).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD


def test_revision_field_is_rejected_on_non_repository_mapping(tmp_path: Path) -> None:
    spec = _repository_spec(
        type="html",
        root_key="repository",
        revision_field="representations.code.revision",
    )
    for key in ("commit_field", "license_field", "status_field"):
        spec.pop(key)

    with pytest.raises(AdapterError, match="valid only for repository") as captured:
        _adapter(tmp_path, code=_valid_code(revision="v1"), spec=spec).load()

    assert captured.value.code == AdapterErrorCode.INVALID_RECORD
    assert captured.value.details["options"] == ["revision_field"]


def test_formal_import_persists_generic_repository_profile_metadata(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path, code=_valid_code())
    raw_source: dict[str, Any] = {
        "id": "generic-repository-metadata",
        "label": "Generic repository metadata",
        "adapter": "generic_manifest",
        "root": str(tmp_path),
        "manifest": "manifest.json",
        "domain": "test-domain",
        "expected_records": 1,
        "read_only": True,
        "asset_roots": {"repository": "."},
        "options": {
            "record_selector": "items",
            "representations": [_repository_spec()],
        },
    }
    source = LoadedSource(
        source_id="generic-repository-metadata",
        label="Generic repository metadata",
        domain="test-domain",
        adapter="generic_manifest",
        root=tmp_path,
        manifest=manifest,
        expected_records=1,
        read_only=True,
        raw=raw_source,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        formal.run_formal_import(
            session,
            [source],
            known_domain_slugs={"test-domain"},
        )
        session.commit()

        profile = session.scalar(select(RepositoryProfile))
        assert profile is not None
        assert profile.pinned_revision == COMMIT.lower()
        assert profile.license_spdx == "Apache"
        assert profile.code_status == "official_local"
        representation = session.scalar(
            select(Representation).where(
                Representation.representation_type == "repository"
            )
        )
        assert representation is not None
        assert representation.revision == COMMIT.lower()
