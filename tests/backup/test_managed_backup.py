from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.backup import manager, workflow
from packages.backup.rehearsal import discover_restored_layout, verify_restored_manifest


class FixtureManager:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def ensure_repository(self) -> dict[str, str]:
        return {"repository": "fixture-only"}

    def backup(self, paths: list[Path], *, tags: list[str]) -> SimpleNamespace:
        self.paths = paths
        return SimpleNamespace(snapshot_id="fixture", files_new=3, bytes_added=20)

    def verify_snapshot(self, snapshot_id: str) -> dict[str, str]:
        assert snapshot_id == "fixture"
        return {
            "status": "verified",
            "snapshot_id": snapshot_id,
            "repository_check": "verified",
            "verification_scope": "snapshot-storage-integrity",
            "data_read": "all-repository-data",
            "restore_rehearsal": "not_run",
        }

    def apply_retention(self) -> None:
        pytest.fail("Backups must not delete historical snapshots")


def fixture_backup(tmp_path: Path, monkeypatch, *, external_inputs=None):
    project = tmp_path / "read-only-install"
    data = tmp_path / "user-data"
    config = data / "config"
    library = data / "library"
    config.mkdir(parents=True, exist_ok=True)
    library.mkdir(exist_ok=True)
    (config / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (library / "paper.pdf").write_bytes(b"managed PDF fixture")
    catalog = project / "outputs/open-catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "catalog.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        workflow, "_postgres_dump", lambda _project, target: target.write_bytes(b"PG fixture")
    )
    fake = FixtureManager()
    result = workflow.create_platform_backup(
        project, fake, data_dir=data, external_inputs=external_inputs
    )
    return project, data, result, fake


def test_managed_backup_is_self_contained_writable_and_preserves_history(tmp_path, monkeypatch):
    project, data, result, _ = fixture_backup(tmp_path, monkeypatch)
    manifest = Path(result["staging_manifest"])
    assert manifest.is_relative_to(data)
    assert not (project / "work").exists()
    assert result["retention_applied"] is False
    assert result["excluded_external_originals"] is True
    assert result["snapshot_verification"]["snapshot_id"] == result["snapshot_id"]
    assert result["snapshot_verification"]["restore_rehearsal"] == "not_run"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema"] == "research-asset-hub.backup/v3"
    assert "library/paper.pdf" in payload["files"]
    assert "outputs/open-catalog/catalog.jsonl" in payload["files"]
    layout = discover_restored_layout(manifest.parent)
    assert verify_restored_manifest(layout)["status"] == "verified"
    (manifest.parent / "library/paper.pdf").write_bytes(b"corrupt")
    assert verify_restored_manifest(layout)["status"] == "failed"


def test_explicit_registered_external_inputs_are_copied_with_nested_roots_deduplicated(
    tmp_path, monkeypatch
):
    source = tmp_path / "frozen-originals"
    source.mkdir()
    paper = source / "original.pdf"
    paper.write_bytes(b"original")
    _, _, result, _ = fixture_backup(
        tmp_path, monkeypatch, external_inputs={"source_root": source, "paper": paper}
    )
    manifest = Path(result["staging_manifest"])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["external_originals"] == {"source_root": "external/source_root"}
    assert payload["excluded_external_originals"] is False
    assert (manifest.parent / "external/source_root/original.pdf").read_bytes() == b"original"
    assert paper.read_bytes() == b"original"


def test_creation_requires_a_real_snapshot_before_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FixtureManager,
        "backup",
        lambda *args, **kwargs: SimpleNamespace(snapshot_id=None, files_new=0, bytes_added=0),
    )
    monkeypatch.setattr(
        FixtureManager, "verify_snapshot", lambda *_: pytest.fail("snapshot was absent")
    )
    with pytest.raises(RuntimeError, match="without a snapshot ID"):
        fixture_backup(tmp_path, monkeypatch)


def test_creation_does_not_report_verified_when_snapshot_verification_fails(tmp_path, monkeypatch):
    def fail_verification(self, snapshot_id):
        assert snapshot_id == "fixture"
        raise RuntimeError("snapshot data is corrupt")

    monkeypatch.setattr(FixtureManager, "verify_snapshot", fail_verification)
    with pytest.raises(RuntimeError, match="snapshot data is corrupt"):
        fixture_backup(tmp_path, monkeypatch)


@pytest.mark.parametrize("unsafe", ["../outside", "/outside", "C:/outside", "config/../../outside"])
def test_v3_manifest_rejects_paths_outside_restore_tree(tmp_path, monkeypatch, unsafe):
    _, _, result, _ = fixture_backup(tmp_path, monkeypatch)
    manifest = Path(result["staging_manifest"])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][unsafe] = {"size": 0, "sha256": "0" * 64}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    validation = verify_restored_manifest(discover_restored_layout(manifest.parent))
    assert validation["status"] == "failed"
    assert {"path": unsafe, "reason": "unsafe-path"} in validation["mismatches"]


def test_v3_verification_rejects_unlisted_managed_files(tmp_path, monkeypatch):
    _, _, result, _ = fixture_backup(tmp_path, monkeypatch)
    root = Path(result["staging_manifest"]).parent
    (root / "library/unlisted.pdf").write_bytes(b"unexpected")
    validation = verify_restored_manifest(discover_restored_layout(root))
    assert {"path": "library/unlisted.pdf", "reason": "unlisted-file"} in validation["mismatches"]


def test_restic_prefers_bundled_executable_before_system_path(tmp_path, monkeypatch):
    module = tmp_path / "app/packages/backup/manager.py"
    bundled = tmp_path / "runtime/restic.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"fixture")
    monkeypatch.delenv("HUB_RESTIC_PATH", raising=False)
    monkeypatch.setattr(manager, "__file__", str(module))
    monkeypatch.setattr(manager.shutil, "which", lambda _: str(tmp_path / "system-restic.exe"))
    assert manager._find_restic() == bundled
