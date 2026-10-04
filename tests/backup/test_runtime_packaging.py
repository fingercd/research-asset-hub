from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "desktop/scripts"


def load_script(filename: str):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_tree_digest_tracks_contents_and_relative_layout(tmp_path):
    digest = load_script("runtime-tree-digest.py").tree_digest
    (tmp_path / "bin").mkdir()
    file = tmp_path / "bin/pg_ctl.exe"
    file.write_bytes(b"fixture")
    first = digest(tmp_path)
    file.write_bytes(b"changed")
    assert digest(tmp_path) != first
    file.write_bytes(b"fixture")
    file.rename(tmp_path / "bin/pg_dump.exe")
    assert digest(tmp_path) != first


def test_personal_copy_requires_unchanged_frozen_root_mapping(tmp_path, monkeypatch):
    check = load_script("validate-source-mapping.py").check_mapping
    source = tmp_path / "source-workspace"
    copied = tmp_path / "user-data/config"
    (source / "config").mkdir(parents=True)
    copied.mkdir(parents=True)
    yaml = (
        "schema_version: 1\nworkspace_root: ..\nsources:\n"
        "  - id: fixture\n    root: originals\n    manifest: manifest.json\n"
        "    adapter: generic_manifest\n    read_only: true\n"
    )
    reference = source / "config/sources.yaml"
    candidate = copied / "sources.yaml"
    reference.write_text(yaml, encoding="utf-8")
    candidate.write_text(yaml, encoding="utf-8")
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "fixture-only-managed-data"))
    check(reference, candidate, source)
    with pytest.raises(ValueError, match="root mappings"):
        check(reference, candidate, tmp_path)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows packaging entry")
def test_stage_validate_only_checks_binary_hashes_without_building(tmp_path):
    binary = tmp_path / "artifact.bin"
    binary.write_bytes(b"fixture")
    notices = tmp_path / "notices.txt"
    notices.write_text("Fixture notices only", encoding="utf-8")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    args = ["pwsh", "-NoProfile", "-File", str(SCRIPTS / "stage-runtime.ps1")]
    for name, value in {
        "PostgresZip": binary,
        "PostgresVersion": "fixture",
        "PostgresSha256": digest,
        "MeiliExe": binary,
        "MeiliVersion": "fixture",
        "MeiliSha256": digest,
        "ResticExe": binary,
        "ResticVersion": "fixture",
        "ResticSha256": digest,
        "ThirdPartyNotices": notices,
    }.items():
        args.extend([f"-{name}", str(value)])
    args.append("-ValidateOnly")
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "no stage/runtime/build changes made" in result.stdout
    args[args.index("-ResticSha256") + 1] = "0" * 64
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
