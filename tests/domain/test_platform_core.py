from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from packages.domain import (
    AnnotationAnchor,
    BackupRunData,
    CodeLineAnchor,
    FieldResolutionRequest,
    IdentityCandidatePayload,
    IntegrationConfigData,
    JobRequest,
    MaterializedAssetCandidate,
    NormalizedMaterializationBatch,
    NormalizedRecord,
    RepositoryProfileData,
    SyncConflictData,
)
from packages.domain.identifiers import new_uuid7


def test_repository_profile_requires_a_stable_identity() -> None:
    profile = RepositoryProfileData(
        host="github.com",
        owner="openai",
        name="example",
        pinned_revision="deadbeef",
    )
    assert profile.owner == "openai"

    with pytest.raises(ValidationError, match="repository profile requires"):
        RepositoryProfileData()


def test_materialization_batch_supports_shared_repository_asset() -> None:
    record = NormalizedRecord(
        source_id="agent",
        source_record_id="paper-1",
        adapter_name="agent",
        asset={"title": "Paper"},
    )
    repository = MaterializedAssetCandidate(
        source_role="repository",
        asset={"asset_type": "repository", "title": "owner/repo"},
        repository_profile={"canonical_url": "https://github.com/owner/repo"},
        representations=[
            {
                "representation_type": "repository",
                "materialization_kind": "directory",
                "root_key": "repository",
                "path_ref": "owner--repo",
                "revision": "deadbeef",
                "tree_digest": "b" * 64,
            }
        ],
    )
    batch = NormalizedMaterializationBatch(
        record=record,
        related_assets=[repository],
    )

    assert batch.related_assets[0].asset.asset_type == "repository"
    assert (
        batch.related_assets[0].representations[0].materialization_kind.value
        == "directory"
    )


def test_typed_anchor_discriminates_and_validates_code_lines() -> None:
    adapter = TypeAdapter(AnnotationAnchor)
    anchor = adapter.validate_python(
        {
            "kind": "code_lines",
            "path_ref": r"src\main.py",
            "line_start": 10,
            "line_end": 14,
            "representation_digest": "a" * 64,
        }
    )

    assert isinstance(anchor, CodeLineAnchor)
    assert anchor.path_ref == "src/main.py"

    with pytest.raises(ValidationError, match="line_end"):
        adapter.validate_python(
            {
                "kind": "text_lines",
                "line_start": 8,
                "line_end": 3,
            }
        )
    with pytest.raises(ValidationError, match="path_ref"):
        adapter.validate_python(
            {
                "kind": "code_lines",
                "path_ref": "../secret.py",
                "line_start": 1,
                "line_end": 1,
            }
        )


def test_field_resolution_requires_claim_for_claim_selection() -> None:
    entity_id = new_uuid7()
    with pytest.raises(ValidationError, match="selected_claim_id"):
        FieldResolutionRequest(
            entity_type="asset",
            entity_id=entity_id,
            field_path="/title",
            resolution_kind="claim",
        )

    resolution = FieldResolutionRequest(
        entity_type="asset",
        entity_id=entity_id,
        field_path="/title",
        resolution_kind="claim",
        selected_claim_id=new_uuid7(),
    )
    assert resolution.selected_claim_id is not None


def test_identity_candidate_rejects_self_match() -> None:
    asset_id = new_uuid7()
    with pytest.raises(ValidationError, match="must be distinct"):
        IdentityCandidatePayload(
            left_asset_id=asset_id,
            right_asset_id=asset_id,
            reason="same DOI",
        )


def test_job_integration_conflict_and_backup_payloads_are_json_safe() -> None:
    job = JobRequest(job_type="extract_pdf", parameters={"pages": [1, 2]})
    integration = IntegrationConfigData(
        provider="zotero",
        name="personal-library",
        config={"library_id": "123"},
    )
    conflict = SyncConflictData(
        external_object_id=new_uuid7(),
        entity_type="asset",
        entity_id=new_uuid7(),
        field_path="/title",
        base_value="Old",
        hub_value="Hub",
        external_value="Zotero",
    )
    backup = BackupRunData(destination_label="local-copy")

    assert job.model_dump(mode="json")["parameters"]["pages"] == [1, 2]
    assert integration.provider == "zotero"
    assert conflict.status.value == "pending"
    assert backup.local_copy_only is True


def test_uuid7_contract_still_rejects_uuid4_in_new_payloads() -> None:
    with pytest.raises(ValidationError, match="UUIDv7"):
        IdentityCandidatePayload(
            left_asset_id=uuid4(),
            right_asset_id=new_uuid7(),
            reason="invalid id",
        )
