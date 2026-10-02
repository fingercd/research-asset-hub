from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.config import PROJECT_ROOT
from packages.knowledge.domains import matches_domain, normalize_domain, source_domain_candidates


@pytest.mark.parametrize(
    "value, expected",
    [
        ("形式化数学", "ai-for-math"),
        ("AI for Mathematics", "ai-for-math"),
        ("视频生成", "video-generation"),
        ("video generation", "video-generation"),
        ("image generation", "image-editing"),
        ("导航压缩", "embodied-ai-compression"),
        ("generation", "generation"),
    ],
)
def test_domain_aliases_prefer_visible_domains(value, expected):
    assert normalize_domain(value, PROJECT_ROOT / "config/taxonomy.yaml") == expected


def test_unknown_and_ambiguous_aliases_are_explicit(tmp_path: Path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "domains:\n  - {id: one, aliases: [shared]}\n  - {id: two, aliases: [shared]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        normalize_domain("shared", path)
    with pytest.raises(ValueError, match="Unknown"):
        normalize_domain("../bad", path)


def test_presentation_filter_keeps_original_source_classification():
    asset = SimpleNamespace(
        domain_slugs=["generation"], title="Video diffusion", tags=[], attributes={}
    )
    assert matches_domain(asset, "video-generation")
    assert not matches_domain(asset, "image-editing")
    assert matches_domain(asset, "generation")
    assert asset.domain_slugs == ["generation"]
    assert source_domain_candidates("video-generation") == ("video-generation", "generation")
