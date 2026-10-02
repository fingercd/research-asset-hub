from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from packages.ingestion import safe_tree_policy
from tools.external_asset_copy import copy_assets
from tools.external_asset_copy.copy_assets import (
    APPLY_CONFIRMATION,
    CopyPlanError,
    apply_plan,
    build_plan,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_packaged_safe_tree_policy_preserves_golden_digest_and_skip_classes(
    tmp_path: Path,
) -> None:
    assert copy_assets._scan_directory is safe_tree_policy.scan_directory
    assert copy_assets._content_tree_digest is safe_tree_policy.content_tree_digest
    ledger = [
        {
            "relative_path": "A.txt",
            "size_bytes": 1,
            "sha256": _sha(b"A"),
        },
        {
            "relative_path": "sub/b.bin",
            "size_bytes": 2,
            "sha256": _sha(b"BC"),
        },
    ]
    assert copy_assets._content_tree_digest(ledger) == (
        "473656a6ecc922c575a9ab47528262a34d9881c7fc16241c28123a91b2291794"
    )

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("safe\n", encoding="utf-8")
    (root / ".env").write_text("secret\n", encoding="utf-8")
    (root / "client.pem").write_text("certificate\n", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"weights")
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    files = copy_assets._scan_directory(root, root, skipped, errors)

    assert not errors
    assert [item["relative_path"] for item in files] == ["src/main.py"]
    assert {
        item["relative_path"]: item["reason"] for item in skipped
    } == {
        ".env": "sensitive_name",
        ".git": "excluded_directory",
        "client.pem": "sensitive_name",
        "model.safetensors": "excluded_weight_or_checkpoint",
    }


def _write_preflight(
    path: Path,
    source: Path,
    destination: Path,
    targets: list[dict[str, object]],
) -> None:
    payload = {
        "schema_version": 1,
        "report_kind": "external-root-relocation-preflight",
        "scope": {"unique_formal_targets": len(targets)},
        "root_mappings": [
            {
                "source_root": str(source),
                "planned_core_root": str(destination),
            }
        ],
        "formal_targets": targets,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _file_target(source: Path, destination: Path, relative: str, data: bytes) -> dict[str, object]:
    return {
        "source_root": str(source),
        "relative_path": relative,
        "planned_core_path": str(destination / Path(*relative.split("/"))),
        "kind": "file",
        "size_bytes": len(data),
        "sha256": _sha(data),
        "references": [],
    }


def test_file_plan_and_apply_are_hash_verified_and_idempotent(tmp_path: Path) -> None:
    core = tmp_path / "core"
    source = tmp_path / "source"
    destination = core / "moved"
    source.mkdir()
    core.mkdir()
    data = b"paper"
    (source / "paper.pdf").write_bytes(data)
    preflight = tmp_path / "preflight.json"
    _write_preflight(
        preflight, source, destination, [_file_target(source, destination, "paper.pdf", data)]
    )

    plan = build_plan(preflight, core)
    assert plan["summary"]["would_copy_file_count"] == 1
    assert not plan["errors"]
    applied = apply_plan(plan, APPLY_CONFIRMATION)
    assert (destination / "paper.pdf").read_bytes() == data
    assert applied["summary"]["copied_and_verified_count"] == 1

    second = build_plan(preflight, core)
    assert second["summary"]["already_present_identical_count"] == 1
    assert second["summary"]["would_copy_file_count"] == 0


def test_apply_refuses_content_conflict_without_overwrite(tmp_path: Path) -> None:
    core = tmp_path / "core"
    source = tmp_path / "source"
    destination = core / "moved"
    source.mkdir()
    destination.mkdir(parents=True)
    data = b"expected"
    (source / "paper.pdf").write_bytes(data)
    target = destination / "paper.pdf"
    target.write_bytes(b"user data")
    preflight = tmp_path / "preflight.json"
    _write_preflight(
        preflight, source, destination, [_file_target(source, destination, "paper.pdf", data)]
    )

    plan = build_plan(preflight, core)
    assert plan["errors"][0]["reason"] == "destination_conflict"
    with pytest.raises(CopyPlanError):
        apply_plan(plan, APPLY_CONFIRMATION)
    assert target.read_bytes() == b"user data"


def test_recursive_directory_skips_sensitive_dependency_and_weight_files(tmp_path: Path) -> None:
    core = tmp_path / "core"
    source = tmp_path / "source"
    destination = core / "moved"
    repo = source / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "src").mkdir()
    core.mkdir()
    (repo / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    (repo / "src" / "token_integration.yaml").write_text(
        "mode: visual-token-integration\n", encoding="utf-8"
    )
    (repo / ".env").write_text("DO_NOT_READ=secret", encoding="utf-8")
    (repo / "auth.json").write_text("secret", encoding="utf-8")
    (repo / "model.safetensors").write_bytes(b"weights")
    (repo / ".git" / "config").write_text("secret", encoding="utf-8")
    (repo / "data" / "private.csv").write_text("secret", encoding="utf-8")
    preflight = tmp_path / "preflight.json"
    targets = [
        {
            "source_root": str(source),
            "relative_path": "repo",
            "planned_core_path": str(destination / "repo"),
            "kind": "directory",
            "tree_digest": "legacy",
            "file_count": 6,
            "aggregate_size_bytes": 1,
            "references": [],
        }
    ]
    _write_preflight(preflight, source, destination, targets)

    plan = build_plan(preflight, core)
    ledger_names = {item["source_relative_path"] for item in plan["copy_ledger"]}
    assert ledger_names == {"repo/src/main.py", "repo/src/token_integration.yaml"}
    skipped_names = {item["relative_path"] for item in plan["skipped_without_content_read"]}
    assert {
        "repo/.env",
        "repo/auth.json",
        "repo/model.safetensors",
        "repo/.git",
        "repo/data",
    } <= skipped_names
    applied = apply_plan(plan, APPLY_CONFIRMATION)
    assert applied["apply_directory_verification"][0]["status"] == "already_present_identical"
    assert (destination / "repo" / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')"
    assert (destination / "repo" / "src" / "token_integration.yaml").is_file()
    assert not (destination / "repo" / ".env").exists()


def test_html_dependencies_are_local_recursive_and_allowlisted(tmp_path: Path) -> None:
    core = tmp_path / "core"
    source = tmp_path / "source"
    destination = core / "moved"
    (source / "assets").mkdir(parents=True)
    core.mkdir()
    html_data = (
        b"<link rel='stylesheet' href='assets/site.css'>"
        b"<script src='assets/app.js'></script><img src='assets/p.png' "
        b"style='background:url(assets/bg.png)'>"
    )
    (source / "guide.html").write_bytes(html_data)
    (source / "assets" / "site.css").write_text("body{background:url(bg.png)}", encoding="utf-8")
    (source / "assets" / "app.js").write_text("import './module.js';", encoding="utf-8")
    (source / "assets" / "module.js").write_text("export const ok=true;", encoding="utf-8")
    (source / "assets" / "p.png").write_bytes(b"png")
    (source / "assets" / "bg.png").write_bytes(b"bg")
    preflight = tmp_path / "preflight.json"
    _write_preflight(
        preflight, source, destination, [_file_target(source, destination, "guide.html", html_data)]
    )

    plan = build_plan(preflight, core)
    dependency_names = {item["relative_path"] for item in plan["html_dependencies"]}
    assert dependency_names == {
        "assets/app.js",
        "assets/bg.png",
        "assets/module.js",
        "assets/p.png",
        "assets/site.css",
    }
    assert plan["summary"]["unique_file_operation_count"] == 6
    assert not plan["dependency_issues"]


def test_html_dependency_outside_root_is_rejected(tmp_path: Path) -> None:
    core = tmp_path / "core"
    source = tmp_path / "source"
    destination = core / "moved"
    source.mkdir()
    core.mkdir()
    (tmp_path / "secret.js").write_text("secret", encoding="utf-8")
    html_data = b"<script src='../secret.js'></script>"
    (source / "guide.html").write_bytes(html_data)
    preflight = tmp_path / "preflight.json"
    _write_preflight(
        preflight, source, destination, [_file_target(source, destination, "guide.html", html_data)]
    )

    plan = build_plan(preflight, core)
    assert not plan["html_dependencies"]
    assert plan["dependency_issues"][0]["reason"] == "outside_source_root"


def test_symlink_is_never_followed(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unsupported")
    core = tmp_path / "core"
    source = tmp_path / "source"
    destination = core / "moved"
    repo = source / "repo"
    outside = tmp_path / "outside"
    repo.mkdir(parents=True)
    outside.mkdir()
    core.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, repo / "linked", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted")
    preflight = tmp_path / "preflight.json"
    targets = [
        {
            "source_root": str(source),
            "relative_path": "repo",
            "planned_core_path": str(destination / "repo"),
            "kind": "directory",
            "tree_digest": "legacy",
            "file_count": 1,
            "aggregate_size_bytes": 1,
            "references": [],
        }
    ]
    _write_preflight(preflight, source, destination, targets)

    plan = build_plan(preflight, core)
    assert not plan["copy_ledger"]
    assert plan["skipped_without_content_read"][0]["reason"] == "reparse_point"
