from pathlib import Path

import pytest
import yaml

from apps.asset_gateway.registry import SourceConfigurationError, SourceRegistry


def test_fast_registry_loader_still_rejects_python_object_tags(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text("!!python/object/apply:builtins.str [unsafe]\n", encoding="utf-8")
    with pytest.raises(SourceConfigurationError) as error:
        SourceRegistry.load(config)
    assert isinstance(error.value.__cause__, yaml.constructor.ConstructorError)
