from __future__ import annotations

from pathlib import Path
from uuid import UUID

from packages.domain.scope_policy import DomainScopePolicy


def test_scope_policy_filters_by_title_and_exact_asset_id(tmp_path: Path) -> None:
    exact_id = UUID("11111111-1111-1111-1111-111111111111")
    path = tmp_path / "scope.yaml"
    path.write_text(
        """schema: research-asset-hub.domain-scope-exclusions/v1
rules:
  - id: exclude-uav
    domain: vision-language-navigation
    reason: user scope
    title_patterns:
      - '\\bUAVs?\\b'
    asset_ids:
      - 11111111-1111-1111-1111-111111111111
""",
        encoding="utf-8",
    )
    policy = DomainScopePolicy.load(path)

    assert policy.filter_domains(
        ["vision-language-navigation", "robotics"],
        asset_id=None,
        title="A UAV Navigation Paper",
    ) == ["robotics"]
    assert policy.filter_domains(
        ["vision-language-navigation"],
        asset_id=exact_id,
        title="Repository Without UAV In Its Name",
    ) == []
    assert policy.filter_domains(
        ["vision-language-navigation"],
        asset_id=None,
        title="General Embodied Navigation Foundation Model",
    ) == ["vision-language-navigation"]
