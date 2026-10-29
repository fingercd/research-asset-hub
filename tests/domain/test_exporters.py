from __future__ import annotations

import json
from pathlib import Path

from packages.domain.exporters import export_jsonl, export_markdown, export_ro_crate
from packages.domain.schemas import NormalizedRecord


def sample_record() -> NormalizedRecord:
    return NormalizedRecord(
        source_id="world_model",
        source_record_id="wm-001",
        source_path="library/catalog/papers.json",
        adapter_name="world_model",
        adapter_version="1.0",
        asset={
            "asset_type": "paper",
            "title": "Learning a World Model",
            "title_zh": "学习世界模型",
            "authors": ["A. Researcher"],
            "year": 2025,
            "abstract": "A compact abstract.",
            "domain_slugs": ["world-model"],
            "external_identifiers": [{"scheme": "doi", "value": "10.1234/example"}],
        },
        representations=[
            {
                "representation_type": "pdf",
                "role": "original",
                "root_key": "pdf",
                "path_ref": "wm-001.pdf",
                "mime_type": "application/pdf",
                "sha256": "a" * 64,
            }
        ],
        relations=[
            {
                "relation_type": "cites",
                "object": {
                    "external_identifier": {
                        "scheme": "doi",
                        "value": "10.9999/earlier",
                    }
                },
                "evidence": {
                    "source": "manifest",
                    "field": "before",
                    "excerpt": "Earlier work",
                },
                "confidence": 1.0,
                "confirmed": True,
            }
        ],
    )


def platform_state(record: NormalizedRecord) -> tuple[list[dict], list[dict]]:
    asset_id = str(record.asset.asset_id)
    return (
        [{"asset_id": asset_id, "status": "read", "user_id": "local"}],
        [
            {
                "id": "note-1",
                "asset_id": asset_id,
                "kind": "comment",
                "title": "我的批语",
                "body_markdown": "这篇论文的关键是**隐状态**。",
                "anchor": {"page": 3},
            }
        ],
    )


def test_jsonl_export_is_lossless_and_utf8(tmp_path: Path) -> None:
    record = sample_record()
    states, annotations = platform_state(record)
    path = export_jsonl(
        [record],
        tmp_path / "catalog.jsonl",
        reading_states=states,
        annotations=annotations,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    documents = [json.loads(line) for line in lines]
    assert [document["record_type"] for document in documents] == [
        "normalized_record",
        "reading_state",
        "annotation",
    ]
    assert documents[0]["payload"]["asset"]["title_zh"] == "学习世界模型"
    assert documents[0]["payload"]["representations"][0]["path_ref"] == "wm-001.pdf"
    assert documents[2]["payload"]["body_markdown"].endswith("**隐状态**。")


def test_markdown_export_contains_source_state_and_annotation(tmp_path: Path) -> None:
    record = sample_record()
    states, annotations = platform_state(record)
    paths = export_markdown(
        [record],
        tmp_path / "markdown",
        reading_states=states,
        annotations=annotations,
    )

    assert paths == [tmp_path / "markdown" / f"{record.asset.asset_id}.md"]
    text = paths[0].read_text(encoding="utf-8")
    assert 'reading_status: "read"' in text
    assert "world_model:pdf/wm-001.pdf" in text
    assert "这篇论文的关键是**隐状态**。" in text
    assert "10.1234/example" in text


def test_ro_crate_export_links_asset_representation_and_personal_state(
    tmp_path: Path,
) -> None:
    record = sample_record()
    states, annotations = platform_state(record)
    path = export_ro_crate(
        [record],
        tmp_path / "crate",
        reading_states=states,
        annotations=annotations,
    )

    document = json.loads(path.read_text(encoding="utf-8"))
    graph = document["@graph"]
    asset_uri = f"urn:uuid:{record.asset.asset_id}"
    asset_node = next(node for node in graph if node["@id"] == asset_uri)
    representation_uri = "source://world_model/pdf/wm-001.pdf"

    assert document["@context"].endswith("/1.1/context")
    assert {"@id": representation_uri} in asset_node["hasPart"]
    assert any(node.get("@type") == "Relationship" for node in graph)
    assert any(node.get("@type") == "Action" for node in graph)
    assert any(node.get("@type") == "Comment" for node in graph)
