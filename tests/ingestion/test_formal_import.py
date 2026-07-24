from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.cli.config import LoadedSource
from packages.domain.models import Asset, Base, FieldResolution, Representation
from packages.domain.schemas import NormalizedRecord
from packages.ingestion import formal


def _loaded_source(root: Path, source_id: str) -> LoadedSource:
    return LoadedSource(
        source_id=source_id,
        label=source_id,
        domain="test-domain",
        adapter="generic_manifest",
        root=root,
        manifest=root / "manifest.json",
        expected_records=1,
        read_only=True,
        raw={
            "id": source_id,
            "label": source_id,
            "domain": "test-domain",
            "adapter": "generic_manifest",
            "root": str(root),
            "manifest": "manifest.json",
            "asset_roots": {"pdf": "."},
        },
    )


def _record(source_id: str, pdf_name: str) -> NormalizedRecord:
    return NormalizedRecord.model_validate(
        {
            "source_id": source_id,
            "source_record_id": "paper-1",
            "adapter_name": "test",
            "asset": {
                "title": "A Shared Paper",
                "authors": ["A. Researcher"],
                "year": 2025,
                "domain_slugs": ["test-domain"],
                "external_identifiers": [
                    {"scheme": "doi", "value": "10.1234/shared"}
                ],
            },
            "representations": [
                {
                    "representation_type": "pdf",
                    "root_key": "pdf",
                    "path_ref": pdf_name,
                },
                {
                    "representation_type": "repository",
                    "url": "https://github.com/example/shared.git",
                },
            ],
            "relations": [
                {
                    "relation_type": "implemented_by",
                    "object": {
                        "external_identifier": {
                            "scheme": "github",
                            "value": "https://github.com/example/shared",
                        }
                    },
                    "evidence": {"source": "manifest", "field": "repository"},
                    "confirmed": True,
                }
            ],
            "raw": {"id": "paper-1"},
        }
    )


def test_formal_import_is_idempotent_and_materializes_repository(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    roots = [tmp_path / "left", tmp_path / "right"]
    for index, root in enumerate(roots):
        root.mkdir()
        (root / f"paper-{index}.pdf").write_bytes(b"%PDF-1.4\n")
    sources = [
        _loaded_source(roots[0], "left"),
        _loaded_source(roots[1], "right"),
    ]
    records = [_record("left", "paper-0.pdf"), _record("right", "paper-1.pdf")]
    monkeypatch.setattr(formal, "_load_all_records", lambda _: records)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = formal.run_formal_import(session, sources)
        session.commit()
        assert first["counters"]["paper_assets_total"] == 1
        assert first["counters"]["repository_assets_total"] == 1
        assert first["counters"]["relations_created"] == 1
        assert first["counters"]["relation_evidence_created"] == 2
        assert formal.database_counts(session) == {
            "assets": 2,
            "papers": 1,
            "repositories": 1,
            "source_records": 2,
            "representations": 4,
            "missing_representations": 0,
            "relations": 1,
            "implemented_by": 1,
            "field_claims": 8,
        }
        derived_representation = session.scalar(
            select(Representation).where(
                Representation.representation_type == "pdf"
            )
        )
        assert derived_representation is not None
        derived_representation.attributes = {
            **dict(derived_representation.attributes or {}),
            "sandbox_policy": "trusted_local",
            "sandbox_policy_source": "static-html-audit/v1",
            "sandbox_policy_content_sha256": "a" * 64,
        }
        session.commit()

        second = formal.run_formal_import(session, sources)
        session.commit()
        assert second["counters"]["paper_assets_created"] == 0
        assert second["counters"]["repository_assets_created"] == 0
        assert second["counters"]["representations_created"] == 0
        assert second["counters"]["relations_created"] == 0
        assert second["counters"]["relation_evidence_created"] == 0
        session.refresh(derived_representation)
        assert derived_representation.attributes["sandbox_policy"] == "trusted_local"
        assert (
            derived_representation.attributes["sandbox_policy_content_sha256"]
            == "a" * 64
        )


def test_repository_url_normalization_keeps_meaningful_subpath() -> None:
    assert (
        formal.canonical_repository_url(
            "HTTPS://GitHub.com/google-research/google-research/tree/master/saycan/"
        )
        == "https://github.com/google-research/google-research/tree/master/saycan"
    )


def test_formal_import_reapplies_active_field_override(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / "override"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    source = _loaded_source(root, "override")
    record = _record("override", "paper.pdf")
    monkeypatch.setattr(formal, "_load_all_records", lambda _: [record])

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        formal.run_formal_import(session, [source])
        session.commit()
        asset = session.scalar(select(Asset).where(Asset.asset_type == "paper"))
        assert asset is not None
        resolution = session.scalar(
            select(FieldResolution).where(
                FieldResolution.entity_id == asset.id,
                FieldResolution.field_path == "/title",
            )
        )
        assert resolution is not None
        resolution.resolution_kind = "override"
        resolution.selected_claim_id = None
        resolution.override_value = "My durable title"
        asset.title = "My durable title"
        session.commit()

        formal.run_formal_import(session, [source])
        session.commit()
        session.refresh(asset)
        assert asset.title == "My durable title"
        session.refresh(resolution)
        assert resolution.resolution_kind == "override"
        assert resolution.override_value == "My durable title"


def test_container_source_path_is_persisted_as_windows_path(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_CONTAINER_WORKSPACE_ROOT", "/workspace")
    monkeypatch.setenv("HUB_HOST_WORKSPACE_ROOT", "D:\\Research")
    assert (
        formal._persisted_source_path(Path("/workspace/domain/paper.pdf"))
        == "D:\\Research\\domain\\paper.pdf"
    )
