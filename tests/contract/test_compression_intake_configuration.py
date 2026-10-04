from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from packages.adapters import DEFAULT_REGISTRY
from packages.domain.schemas import SourceConfig
from scripts.build_domain_portal_additions_snapshot import PORTALS, SOURCE_ID, build_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = PROJECT_ROOT.parent.resolve()

EXPECTED_NEW_SOURCES = {
    "video-llm-compression-v1-20260728": 33,
    "video-llm-compression-20260831": 95,
    "video-llm-compression-enrichment-20260831": 1,
    "embodied-ai-compression-20260831": 44,
    "navigation-compression-20260831": 20,
    "domain-portals-20260831": 4,
}
EXPECTED_SUCCESSOR_SOURCES = {
    "intake-streamvln-2026-core-20260831": 1,
    "image-editing-2026-core-20260831": 31,
    "image-editing-anchors-2026-core-20260831": 2,
    "image-editing-2026-pdf-additions-core-20260831": 4,
    "image-editing-2026-code-repos-core-20260831": 14,
    "image-editing-anchors-2026-code-urls-core-20260831": 2,
    "image-editing-anchors-2026-pdf-additions-core-20260831": 2,
}
EXPECTED_TOP_CONFERENCE_SOURCES = {
    "top-conference-20260831-agent": 15,
    "top-conference-20260831-fine-tuning": 2,
    "top-conference-20260831-generation": 15,
    "top-conference-20260831-infra": 183,
    "top-conference-20260831-world-model": 2,
}
EXPECTED_AUXILIARY_SOURCES = {
    "intake-streamvln-2026-core-20260831-v2": 1,
    "navigation-compression-code-enrichment-20260831": 2,
}
EXPECTED_EXISTING_CODE_SOURCES = {
    "existing-code-video-llm-compression-20260831": 2,
    "existing-code-agent-20260831": 8,
    "existing-code-fine-tuning-20260831": 13,
    "existing-code-generation-20260831": 1,
    "existing-code-infra-20260831": 8,
    "existing-code-vision-language-navigation-20260831": 20,
}
EXPECTED_VLN_AUDIT_SOURCES = {
    "vln-2026-update-20260904": 23,
    "vln-agentvln-code-refresh-20260904": 1,
    "vln-2026-backfill-20260904": 42,
    "vln-release-status-audit-20260904": 75,
}
LEGACY_GUIDE_SUCCESSOR = "intake-streamvln-2026-core-20260831-v2"
EXPECTED_NEW_DOMAINS = {
    "video-llm-compression",
    "embodied-ai-compression",
    "navigation-compression",
}
FROZEN_SOURCE_IDS = {
    "agent",
    "infra",
    "world_model",
    "reinforcement_learning",
    "fine_tuning",
    "generation",
    "llm_tech_reports",
    "vln-atlas-20260726",
    "game-rl-atlas-20260724",
    "wsvad-atlas-20260724",
    "domain-portals-20260726",
    "intake-awarevln-2026",
    "intake-streamvln-2026",
    "intake-internvla-n1-2026",
    "image-editing-2026",
    "image-editing-anchors-2026",
    "image-editing-2026-pdf-additions",
    "image-editing-2026-code-repos",
    "image-editing-anchors-2026-code-urls",
    "image-editing-anchors-2026-pdf-additions",
}
EXPECTED_FROZEN_RECORDS = {
    "agent": 72,
    "infra": 71,
    "world_model": 46,
    "reinforcement_learning": 108,
    "fine_tuning": 67,
    "generation": 64,
    "llm_tech_reports": 90,
    "vln-atlas-20260726": 72,
    "game-rl-atlas-20260724": 72,
    "wsvad-atlas-20260724": 45,
    "domain-portals-20260726": 10,
    "intake-awarevln-2026": 1,
    "intake-streamvln-2026": 1,
    "intake-internvla-n1-2026": 1,
    "image-editing-2026": 31,
    "image-editing-anchors-2026": 2,
    "image-editing-2026-pdf-additions": 4,
    "image-editing-2026-code-repos": 14,
    "image-editing-anchors-2026-code-urls": 2,
    "image-editing-anchors-2026-pdf-additions": 2,
}
BASELINE_PORTAL_IDS = {
    "agent",
    "infra",
    "world-model",
    "reinforcement-learning",
    "fine-tuning",
    "generation",
    "llm-tech-reports",
    "vision-language-navigation",
    "game-rl",
    "wsvad",
    "image-editing",
    "video-llm-compression",
    "embodied-ai-compression",
    "navigation-compression",
}


def _configured_sources() -> list[SourceConfig]:
    document = yaml.safe_load(
        (PROJECT_ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    )
    return [SourceConfig.model_validate(value) for value in document["sources"]]


def test_compression_sources_are_additive_with_frozen_roots_and_no_guides() -> None:
    sources = _configured_sources()
    by_id = {source.source_id: source for source in sources}

    expected_live_ids = (
        FROZEN_SOURCE_IDS
        | EXPECTED_NEW_SOURCES.keys()
        | EXPECTED_SUCCESSOR_SOURCES.keys()
        | EXPECTED_TOP_CONFERENCE_SOURCES.keys()
        | EXPECTED_AUXILIARY_SOURCES.keys()
        | EXPECTED_EXISTING_CODE_SOURCES.keys()
        | EXPECTED_VLN_AUDIT_SOURCES.keys()
    )
    assert expected_live_ids <= by_id.keys()
    assert len(sources) == len(by_id)
    assert EXPECTED_FROZEN_RECORDS.keys() == FROZEN_SOURCE_IDS
    assert {
        source_id: by_id[source_id].expected_records for source_id in FROZEN_SOURCE_IDS
    } == EXPECTED_FROZEN_RECORDS
    for source in sources:
        assert source.read_only is True
    assert sum(by_id[source_id].expected_records or 0 for source_id in expected_live_ids) == 1441
    assert {
        source_id: by_id[source_id].expected_records for source_id in EXPECTED_NEW_SOURCES
    } == EXPECTED_NEW_SOURCES
    assert {
        source_id: by_id[source_id].expected_records for source_id in EXPECTED_SUCCESSOR_SOURCES
    } == EXPECTED_SUCCESSOR_SOURCES
    assert {
        source_id: by_id[source_id].expected_records
        for source_id in EXPECTED_TOP_CONFERENCE_SOURCES
    } == EXPECTED_TOP_CONFERENCE_SOURCES
    assert {
        source_id: by_id[source_id].expected_records for source_id in EXPECTED_AUXILIARY_SOURCES
    } == EXPECTED_AUXILIARY_SOURCES
    assert {
        source_id: by_id[source_id].expected_records for source_id in EXPECTED_EXISTING_CODE_SOURCES
    } == EXPECTED_EXISTING_CODE_SOURCES
    assert {
        source_id: by_id[source_id].expected_records for source_id in EXPECTED_VLN_AUDIT_SOURCES
    } == EXPECTED_VLN_AUDIT_SOURCES

    for source_id in (
        EXPECTED_NEW_SOURCES
        | EXPECTED_SUCCESSOR_SOURCES
        | EXPECTED_TOP_CONFERENCE_SOURCES
        | EXPECTED_AUXILIARY_SOURCES
        | EXPECTED_EXISTING_CODE_SOURCES
        | EXPECTED_VLN_AUDIT_SOURCES
    ):
        source = by_id[source_id]
        assert source.read_only is True
        representation_specs = source.options.get("representations", [])
        guide_specs = [spec for spec in representation_specs if spec.get("role") == "guide"]
        if source_id == LEGACY_GUIDE_SUCCESSOR:
            assert guide_specs == [
                {
                    "type": "markdown",
                    "role": "guide",
                    "root_key": "note",
                    "path_field": "representations.note.path",
                    "sha256_field": "representations.note.sha256",
                    "mime_type": "text/markdown",
                }
            ]
        else:
            assert guide_specs == []
        adapter = DEFAULT_REGISTRY.create(source.adapter, source, base_dir=PROJECT_ROOT)
        for root_key, root in source.asset_roots.items():
            candidate = Path(root)
            if not candidate.is_absolute():
                candidate = (PROJECT_ROOT / source.root_path / candidate).resolve()
                assert candidate == CORE_ROOT or CORE_ROOT in candidate.parents
            else:
                candidate = candidate.resolve()
            assert adapter.path_ref(candidate / "fixture.pdf", root_key=root_key) == "fixture.pdf"


def test_top_conference_sidecars_are_complete_and_do_not_forge_repositories() -> None:
    sources = _configured_sources()
    by_id = {source.source_id: source for source in sources}
    allowlist = yaml.safe_load(
        (PROJECT_ROOT / "config" / "check-unit-actions-allowlist.yaml").read_text(encoding="utf-8")
    )
    official_local = 0
    no_local_official_code = 0

    for source_id, expected in EXPECTED_TOP_CONFERENCE_SOURCES.items():
        source = by_id[source_id]
        domain = source.domain_slugs[0]
        repository_specs = [
            spec for spec in source.options["representations"] if spec["type"] == "repository"
        ]
        assert repository_specs == [
            {
                "type": "repository",
                "role": "official",
                "root_key": "repository",
                "path_field": "representations.code.path",
                "url_field": "representations.code.url",
                "commit_field": "representations.code.commit",
                "license_field": "representations.code.license",
                "status_field": "representations.code.status",
                "network_required": True,
            }
        ]
        allowed_no_code_titles = set((allowlist.get(domain) or {}).get("no_code_titles") or [])
        manifest_path = PROJECT_ROOT / source.root_path / source.manifest_path
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = document["items"]
        assert len(items) == expected
        assert len({item["id"] for item in items}) == expected

        adapter = DEFAULT_REGISTRY.create(source.adapter, source, base_dir=PROJECT_ROOT)
        records = adapter.load()
        assert len(records) == expected
        assert all(
            str(candidate.role) != "guide"
            for record in records
            for candidate in record.representations
        )

        for item in items:
            representations = item["representations"]
            assert set(representations) >= {"pdf", "html"}
            assert len(representations["pdf"]["sha256"]) == 64
            assert len(representations["html"]["sha256"]) == 64
            code_status = item["provenance"]["code_materialization_status"]
            if code_status == "official_local":
                official_local += 1
                code = representations["code"]
                assert code["path"]
                assert code["url"].startswith("https://")
                assert len(code["commit"]) == 40
                assert "license" in code
            else:
                no_local_official_code += 1
                assert code_status in {
                    "none_found",
                    "third_party_only",
                    "announced_pending",
                }
                assert "code" not in representations
                assert item["title"] in allowed_no_code_titles

    assert official_local == 126
    assert no_local_official_code == 91


def test_compression_taxonomy_and_portal_registry_are_complete() -> None:
    taxonomy = yaml.safe_load(
        (PROJECT_ROOT / "config" / "taxonomy.yaml").read_text(encoding="utf-8")
    )
    domain_ids = {item["id"] for item in taxonomy["domains"]}
    assert EXPECTED_NEW_DOMAINS <= domain_ids

    portal_index = yaml.safe_load(
        (PROJECT_ROOT / "config" / "domain-portals" / "v2" / "portal-index.yaml").read_text(
            encoding="utf-8"
        )
    )
    portals = {item["slug"]: item for item in portal_index["portals"]}
    assert len(portals) == len(portal_index["portals"])
    assert BASELINE_PORTAL_IDS <= portals.keys()
    assert portals.keys() <= domain_ids
    assert EXPECTED_NEW_DOMAINS <= portals.keys()
    for slug in EXPECTED_NEW_DOMAINS | {"image-editing"}:
        assert portals[slug]["original_selector"]["source_id"] == SOURCE_ID


def test_available_compression_sidecars_match_declared_counts(local_research_assets: None) -> None:
    sources = _configured_sources()
    by_id = {source.source_id: source for source in sources}
    manifests = {
        source_id: (PROJECT_ROOT / by_id[source_id].root_path / by_id[source_id].manifest_path)
        for source_id in EXPECTED_NEW_SOURCES
    }
    if not all(path.is_file() for path in manifests.values()):
        pytest.skip("compression sidecars are generated only after all PDF HTML is materialized")

    for source_id, expected in EXPECTED_NEW_SOURCES.items():
        source = by_id[source_id]
        adapter = DEFAULT_REGISTRY.create(source.adapter, source, base_dir=PROJECT_ROOT)
        records = adapter.load()
        assert len(records) == expected
        assert len({record.source_record_id for record in records}) == expected
        assert all(
            str(candidate.role) != "guide"
            for record in records
            for candidate in record.representations
        )


def test_portal_additions_snapshot_is_deterministic_or_fails_closed() -> None:
    missing = [
        CORE_ROOT / spec.asset_root / spec.entrypoint
        for spec in PORTALS
        if not (CORE_ROOT / spec.asset_root / spec.entrypoint).is_file()
    ]
    if missing:
        with pytest.raises(FileNotFoundError):
            build_snapshot()
        return

    first = build_snapshot()
    second = build_snapshot()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.items) == len(PORTALS) == 4
    assert first.source_id == SOURCE_ID
    assert set(first.domain_slugs) == {spec.slug for spec in PORTALS}
