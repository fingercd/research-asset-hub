from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.domain.schemas import NormalizedRecord, normalized_record_json_schema

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_static_asset_schema_is_valid_json_and_documents_uuid7() -> None:
    schema_path = PROJECT_ROOT / "docs" / "schemas" / "asset.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["asset_id"]["pattern"]
    assert "paper" in schema["properties"]["asset_type"]["oneOf"][0]["enum"]


def test_generated_normalized_record_schema_has_required_components() -> None:
    schema = normalized_record_json_schema()

    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["$defs"]["NormalizedAsset"]
    assert schema["$defs"]["RepresentationCandidate"]
    assert schema["$defs"]["RelationCandidate"]
    assert set(schema["required"]) >= {
        "source_id",
        "source_record_id",
        "adapter_name",
        "asset",
    }


def test_model_schema_round_trip_uses_json_safe_values() -> None:
    record = NormalizedRecord(
        source_id="generation",
        source_record_id="001",
        adapter_name="generation",
        asset={"title": "Generation paper", "source_urls": ["https://example.org/paper"]},
    )

    payload = record.model_dump(mode="json")
    restored = NormalizedRecord.model_validate(payload)
    assert restored == record


def test_sqlalchemy_metadata_contains_required_tables() -> None:
    pytest.importorskip("sqlalchemy")
    from packages.domain.models import Base

    required = {
        "sources",
        "source_records",
        "assets",
        "external_identifiers",
        "representations",
        "relations",
        "reading_states",
        "annotations",
        "import_runs",
        "audit_events",
    }
    assert required <= set(Base.metadata.tables)

    external_constraints = Base.metadata.tables["external_identifiers"].constraints
    assert any(
        getattr(constraint, "name", None) == "uq_external_identifier_authority"
        for constraint in external_constraints
    )
