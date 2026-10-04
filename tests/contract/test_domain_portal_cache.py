"""Portal content versions invalidate caches without waiting for source hashing."""

import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
import yaml

from packages.domain_portals import DomainPortalLoadError, load_domain_portal, loader


def test_portal_caches_content_by_version_and_defers_source_digest(tmp_path, monkeypatch):
    original = Path(__file__).resolve().parents[2] / "config" / "domain-portals" / "v2"
    config = tmp_path / "config"
    directory = config / "world-model"
    shutil.copytree(original / "world-model", directory)
    index = yaml.safe_load((original / "portal-index.yaml").read_text(encoding="utf-8"))
    index["portals"] = [entry for entry in index["portals"] if entry["slug"] == "world-model"]
    (config / "portal-index.yaml").write_text(yaml.safe_dump(index), encoding="utf-8")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "reference.html"
    source.write_text("original reference", encoding="utf-8")
    stat = source.stat()
    metadata_path = directory / "portal.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["source_inputs"] = [
        {
            "path": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    ]
    metadata_path.write_text(yaml.safe_dump(metadata, allow_unicode=True), encoding="utf-8")
    started, release = Event(), Event()
    original_digest = loader._sha256
    reads = []
    original_read = loader._read_yaml

    def count_read(path):
        reads.append(path)
        return original_read(path)

    def blocked_digest(path):
        started.set()
        release.wait(timeout=5)
        return original_digest(path)

    monkeypatch.setattr(loader, "_sha256", blocked_digest)
    monkeypatch.setattr(loader, "_read_yaml", count_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                load_domain_portal, config, source_root, "world-model", defer_source_validation=True
            )
            portal = request.result(timeout=2)
        assert started.wait(timeout=2)
        assert not release.is_set()
        count = len(reads)
        second = load_domain_portal(
            config, source_root, "world-model", defer_source_validation=True
        )
        assert len(reads) == count
        assert second.content_digest == portal.content_digest
        overview = directory / "overview.md"
        overview_content = overview.read_text(encoding="utf-8")
        overview.write_text(overview_content + "\nUpdated evidence.\n", encoding="utf-8")
        updated = load_domain_portal(
            config, source_root, "world-model", defer_source_validation=True
        )
        assert updated.content_digest != portal.content_digest
        assert len(reads) > count
        overview.unlink()
        with pytest.raises(DomainPortalLoadError, match="incomplete"):
            load_domain_portal(config, source_root, "world-model", defer_source_validation=True)
    finally:
        release.set()
    overview.write_text(overview_content, encoding="utf-8")
    source.write_text("changed reference", encoding="utf-8")
    stale = load_domain_portal(config, source_root, "world-model")
    assert stale.stale is True
    assert stale.review_status == "needs_review"
    assert any(reason.reason == "digest_changed" for reason in stale.stale_reasons)
