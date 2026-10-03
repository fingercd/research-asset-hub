from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from packages.domain.enums import RelationType
from packages.domain.identifiers import new_uuid7
from packages.domain.schemas import (
    ExternalIdentifier,
    NormalizedAsset,
    NormalizedRecord,
    RelationCandidate,
    RepresentationCandidate,
    SourceConfig,
)


def test_uuid7_has_expected_version_variant_and_timestamp() -> None:
    timestamp_ms = 1_750_000_000_123
    identifier = new_uuid7(timestamp_ms=timestamp_ms)

    assert identifier.version == 7
    assert identifier.variant == UUID("00000000-0000-7000-8000-000000000000").variant
    assert identifier.int >> 80 == timestamp_ms


def test_asset_rejects_non_uuid7_identifier() -> None:
    with pytest.raises(ValidationError, match="UUIDv7"):
        NormalizedAsset(asset_id=uuid4(), title="Invalid identifier")


def test_core_and_namespaced_asset_types_are_supported() -> None:
    assert NormalizedAsset(title="Paper").asset_type == "paper"
    assert (
        NormalizedAsset(title="Simulation", asset_type="lab:simulation").asset_type
        == "lab:simulation"
    )

    with pytest.raises(ValidationError, match="namespaced extension"):
        NormalizedAsset(title="Typo", asset_type="future_type")


def test_domain_slug_is_open_but_well_formed() -> None:
    asset = NormalizedAsset(title="New field", domain_slugs=["new-field"])
    assert asset.domain_slugs == ["new-field"]

    with pytest.raises(ValidationError, match="domain slug"):
        NormalizedAsset(title="Bad field", domain_slugs=["../../outside"])


def test_external_identifier_is_canonical_and_unique() -> None:
    identifier = ExternalIdentifier(
        scheme="DOI",
        value="https://doi.org/10.1000/ABC",
    )
    assert identifier.scheme == "doi"
    assert identifier.canonical == "10.1000/abc"
    assert identifier.is_identity is True

    with pytest.raises(ValidationError, match="must not contain duplicates"):
        NormalizedAsset(
            title="Duplicate DOI",
            external_identifiers=[
                {"scheme": "doi", "value": "10.1000/ABC"},
                {"scheme": "doi", "value": "https://doi.org/10.1000/abc"},
            ],
        )


def test_external_identifier_scope_distinguishes_zotero_libraries() -> None:
    asset = NormalizedAsset(
        title="Shared item key",
        external_identifiers=[
            {"scheme": "zotero", "scope": "library-a", "value": "ABC123"},
            {"scheme": "zotero", "scope": "library-b", "value": "ABC123"},
            {"scheme": "url", "value": "https://example.org/landing"},
        ],
    )

    assert len(asset.external_identifiers) == 3
    assert asset.external_identifiers[-1].is_identity is False


@pytest.mark.parametrize(
    "path",
    [
        "../paper.pdf",
        "papers/../../paper.pdf",
        "/absolute/paper.pdf",
        r"C:\papers\paper.pdf",
        r"\\server\share\paper.pdf",
    ],
)
def test_representation_rejects_unbounded_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="path_ref"):
        RepresentationCandidate(representation_type="pdf", path_ref=path)


def test_representation_normalizes_windows_separators() -> None:
    representation = RepresentationCandidate(
        representation_type="pdf",
        root_key="pdf",
        path_ref=r"papers\中文 论文.pdf",
    )
    assert representation.path_ref == "papers/中文 论文.pdf"


def test_source_config_accepts_human_yaml_field_names() -> None:
    source = SourceConfig.model_validate(
        {
            "id": "reinforcement_learning",
            "label": "强化学习",
            "adapter": "reinforcement_learning",
            "root": "../强化学习/outputs/强化学习论文研究站",
            "manifest": "data/papers.json",
            "domain": "reinforcement-learning",
            "asset_roots": {"pdf": "../强化学习论文库"},
            "formal_html": ["papers/*.html"],
        }
    )

    assert source.source_id == "reinforcement_learning"
    assert source.root_path.startswith("../强化学习")
    assert source.domain_slugs == ["reinforcement-learning"]
    assert source.asset_roots["pdf"] == "../强化学习论文库"


def test_normalized_record_inherits_source_for_paths_and_relations() -> None:
    record = NormalizedRecord(
        source_id="agent",
        source_record_id="p001",
        adapter_name="agent",
        asset={"title": "A paper"},
        representations=[
            {
                "representation_type": "pdf",
                "root_key": "pdf",
                "path_ref": "paper.pdf",
            }
        ],
        relations=[
            {
                "relation_type": RelationType.CITES,
                "object": {"source_record_id": "p000"},
                "evidence": {"source": "manifest", "field": "before"},
            }
        ],
    )

    assert record.representations[0].source_id == "agent"
    assert record.relations[0].object.source_id == "agent"


def test_unknown_relation_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="relation_type"):
        RelationCandidate(
            relation_type="looks_like",
            object={"source_record_id": "p2"},
            evidence={"source": "test"},
        )


def test_invalid_year_has_a_field_level_error() -> None:
    with pytest.raises(ValidationError) as error:
        NormalizedAsset(title="Future paper", year=2400)
    assert error.value.errors()[0]["loc"] == ("year",)


def test_raw_source_summary_must_be_json_serializable() -> None:
    with pytest.raises(ValidationError, match="raw"):
        NormalizedRecord(
            source_id="agent",
            source_record_id="p001",
            adapter_name="agent",
            asset={"title": "Paper"},
            raw={"unexpected": object()},
        )


@pytest.mark.parametrize("field", ["attributes", "raw"])
def test_normalized_record_rejects_nested_nul_before_database_import(field: str) -> None:
    asset: dict[str, object] = {"title": "Paper"}
    record: dict[str, object] = {
        "source_id": "agent",
        "source_record_id": "p001",
        "adapter_name": "generic_manifest",
        "asset": asset,
    }
    evidence = {"evidence": [{"excerpt": "PDF formula\x00artifact"}]}
    if field == "attributes":
        asset["attributes"] = evidence
    else:
        record["raw"] = evidence
    with pytest.raises(ValidationError, match="contains NUL"):
        NormalizedRecord.model_validate(record)


def test_normalized_record_preserves_unicode_evidence() -> None:
    record = NormalizedRecord(
        source_id="agent",
        source_record_id="p001",
        adapter_name="generic_manifest",
        asset={
            "title": "论文",
            "attributes": {"evidence": [{"excerpt": "中文公式：α ≥ β\n第二行"}]},
        },
    )
    assert record.asset.attributes["evidence"] == [{"excerpt": "中文公式：α ≥ β\n第二行"}]
