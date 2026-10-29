from __future__ import annotations

from uuid import uuid4

from apps.api.routes_v1 import _representation_choice_key, _representation_matches_action
from packages.domain.models import Asset, Representation


def test_unified_guide_is_html_default_ahead_of_legacy_original() -> None:
    paper = Asset(id=uuid4(), asset_type="paper", title="Paper")
    legacy = Representation(
        id=uuid4(),
        asset_id=paper.id,
        representation_type="html",
        role="original",
        availability="available",
        materialization_kind="file",
        path_ref="old.html",
        attributes={},
    )
    unified = Representation(
        id=uuid4(),
        asset_id=paper.id,
        representation_type="html",
        role="guide",
        availability="available",
        materialization_kind="file",
        path_ref="guide.html",
        attributes={"unified_guide": True},
    )
    assert _representation_matches_action(unified, "guide")
    assert _representation_matches_action(unified, "html")
    assert _representation_choice_key((unified, paper, None)) < _representation_choice_key(
        (legacy, paper, None)
    )
