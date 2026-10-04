from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.asset_gateway.registry import SourceConfigurationError, SourceRegistry
from packages.managed_sources.service import (
    load_managed_sources,
    managed_path,
    register_managed_source,
)


def source(data_dir: Path, identifier: str):
    return {
        "id": identifier,
        "label": identifier,
        "domain": "agent",
        "adapter": "generic_manifest",
        "asset_roots": {"pdf": str(data_dir / "library" / "documents" / identifier)},
    }


def test_concurrent_processes_append_without_lost_roots_or_partial_yaml(tmp_path):
    script = """
import sys
from pathlib import Path
from packages.managed_sources.service import register_managed_source
root, identifier = Path(sys.argv[1]), sys.argv[2]
register_managed_source(root, {
    'id': identifier, 'label': identifier, 'domain': 'agent',
    'adapter': 'generic_manifest', 'asset_roots': {'pdf': str(root / 'library' / identifier)},
}, {'items': [{'id': identifier, 'title': identifier}]})
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), f"parallel-{number}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for number in range(6)
    ]
    for process in processes:
        output, error = process.communicate(timeout=30)
        assert process.returncode == 0, (output, error)
    rows = load_managed_sources(tmp_path)
    assert {row["id"] for row in rows} == {f"parallel-{number}" for number in range(6)}
    for row in rows:
        assert (Path(row["root"]) / "manifest.json").read_text(encoding="utf-8").startswith("{")


def test_overlay_reload_failure_keeps_previous_complete_snapshot(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources: []\n", encoding="utf-8")
    data_dir = tmp_path / "user"
    register_managed_source(data_dir, source(data_dir, "kept-source"), {"items": []})
    registry = SourceRegistry.load(config, data_dir=data_dir)
    snapshot = registry._snapshot
    (data_dir / "config" / "managed-sources.yaml").write_text(
        "sources: invalid\n", encoding="utf-8"
    )
    with pytest.raises(SourceConfigurationError):
        registry.refresh_if_changed()
    assert registry._snapshot is snapshot
    assert ("kept-source", "pdf") in snapshot.roots


def test_managed_subdirectory_redirect_is_rejected_before_writes(tmp_path):
    outside, data_dir = tmp_path / "outside", tmp_path / "user"
    outside.mkdir()
    data_dir.mkdir()
    try:
        os.symlink(outside, data_dir / "config", target_is_directory=True)
    except OSError:
        pytest.skip("Windows symbolic-link privilege is unavailable")
    with pytest.raises(ValueError):
        register_managed_source(data_dir, source(data_dir, "blocked-source"), {"items": []})
    assert not list(outside.iterdir())
    with pytest.raises(ValueError):
        managed_path(data_dir, "config", "managed-sources.yaml")
