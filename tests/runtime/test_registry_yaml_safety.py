from pathlib import Path

import pytest
import yaml

from apps.asset_gateway.registry import SourceRegistry


def test_fast_registry_loader_still_rejects_python_object_tags(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text("!!python/object/apply:builtins.str [unsafe]\n", encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError):
        SourceRegistry.load(config)
