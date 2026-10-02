from __future__ import annotations

from pathlib import Path

import yaml

from packages.adapters import GenericManifestAdapter
from packages.domain.schemas import SourceConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_math_source_maps_strong_identity_and_fixed_repository_revision() -> None:
    sources = yaml.safe_load((PROJECT_ROOT / "config" / "sources.yaml").read_text("utf-8"))
    source = next(item for item in sources["sources"] if item["id"] == "ai-for-math-20261002")
    config = SourceConfig.model_validate(
        {
            **source,
            "root": str(PROJECT_ROOT / "tests" / "fixtures" / "ai_for_math"),
            "manifest": "manifest.json",
            "expected_records": 1,
        }
    )
    records = GenericManifestAdapter(config).load()
    assert len(records) == 1
    record = records[0]
    assert record.source_record_id == "formal-math-research-frontier-survey"
    assert record.asset.domain_slugs == ["ai-for-math"]
    assert record.asset.year == 2026
    assert record.asset.authors == ["Jiang, Eric", "Liang, Xiao"]
    assert record.asset.title_zh == "从解题器到研究：研究前沿上的大语言模型驱动形式数学"
    assert any(
        identifier.scheme == "arxiv" and identifier.value == "2607.07779"
        for identifier in record.asset.external_identifiers
    )
    repository = next(
        rep for rep in record.representations if rep.representation_type == "repository"
    )
    assert str(repository.url) == "https://github.com/ericjiang18/Awesome-Formal-Mathematics"
    assert repository.revision == "f42d16da1b567d353bd7beefda6b3cb3017aca9a"
    assert all(rep.role != "guide" for rep in record.representations)
