from __future__ import annotations

from scripts.build_domain_portal_snapshot import PORTALS, SOURCE_ID, build_snapshot


def test_domain_portal_snapshot_is_complete_and_fail_closed(local_research_assets: None) -> None:
    first = build_snapshot()
    second = build_snapshot()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.items) == len(PORTALS) == 10
    assert set(first.domain_slugs) == {spec.slug for spec in PORTALS}
    for item in first.items:
        assert item.asset.asset_type == "collection"
        assert item.asset.attributes["kind"] == "domain-overview"
        assert item.asset.attributes["system_hidden"] is True
        assert len(item.representations) == 1
        representation = item.representations[0]
        assert representation.source_id == SOURCE_ID
        assert representation.metadata["strict_mounts"] is True
        assert any(mount.mount_path == "/" for mount in representation.mounts)
        assert all(mount.source_id == SOURCE_ID for mount in representation.mounts)
