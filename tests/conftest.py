from __future__ import annotations

from pathlib import Path

import pytest

from packages.cli.config import load_sources


def pytest_configure(config: pytest.Config) -> None:
    """Create the parent of the configured basetemp in a fresh checkout.

    Pytest owns basetemp itself; do not rely on a prior Ruff run creating work/.
    """
    basetemp = config.getoption("basetemp")
    if basetemp:
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def isolated_user_library(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Do not merge the developer's managed sources into test-owned registries.

    Individual tests can still override or delete HUB_DATA_DIR with monkeypatch
    when exercising library-selection behavior. Original source checks remain
    enabled through the explicit source configuration below.
    """
    library = tmp_path_factory.mktemp("isolated-user-library")
    monkeypatch.setenv("HUB_DATA_DIR", str(library))
    return library


@pytest.fixture(scope="session")
def local_research_assets(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep original-file checks active where the registered workspace is supplied."""
    config = Path(__file__).resolve().parents[1] / "config/sources.yaml"
    baseline_ids = {
        "agent", "infra", "world_model", "reinforcement_learning",
        "fine_tuning", "generation", "llm_tech_reports",
    }
    # Session fixtures run before function-scoped environment isolation. Keep
    # managed overlays empty here too, without replacing registered originals.
    sources = load_sources(config, data_dir=tmp_path_factory.mktemp("original-source-check"))
    if not any(source.root.is_dir() for source in sources if source.source_id in baseline_ids):
        pytest.skip("requires registered original research assets, which are not shipped in Git")
