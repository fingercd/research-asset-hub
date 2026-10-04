from __future__ import annotations

from pathlib import Path

import pytest

from packages.cli.config import load_sources


@pytest.fixture(scope="session")
def local_research_assets() -> None:
    """Keep original-file checks active where the registered workspace is supplied."""
    config = Path(__file__).resolve().parents[1] / "config/sources.yaml"
    baseline_ids = {
        "agent", "infra", "world_model", "reinforcement_learning",
        "fine_tuning", "generation", "llm_tech_reports",
    }
    sources = load_sources(config)
    if not any(source.root.is_dir() for source in sources if source.source_id in baseline_ids):
        pytest.skip("requires registered original research assets, which are not shipped in Git")
