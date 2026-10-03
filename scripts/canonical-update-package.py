"""Prepare a hash-pinned, file-only update for the canonical Windows installation.

This command only writes the package directory. Deployment is a separate,
administrator-only PowerShell entry point, with a stopped-process gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

INSTALL = Path("C:/Program Files/ResearchAssetHub")
RELEASE_HASH = "83fad1540b935b2de09e3bc432e90f4c1742fc4a7c779e9db39fc934264be504"


def digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Use a fresh output directory; existing packages are immutable.")
    output.mkdir(parents=True)
    payload = output / "payload"
    source_site = root / ".venv/Lib/site-packages"
    validation = root / "work/validation"
    installed = json.loads(
        (validation / "canonical-update-installed-dependencies.json").read_text("utf-8-sig")
    )
    installed_dist = {
        canonicalize_name(name): value for name, value in installed["distributions"].items()
    }
    source_dist = {
        canonicalize_name(dist.metadata["Name"]): dist
        for dist in metadata.distributions(path=[str(source_site)])
    }
    requirements = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"][
        "dependencies"
    ]
    # The desktop/API/worker do not import MCP. The standalone MCP bridge uses
    # the workspace venv, so its dependency family is deliberately excluded.
    workspace_only = {"mcp", "pywin32"}
    queue = [
        Requirement(item)
        for item in requirements
        if canonicalize_name(Requirement(item).name) not in workspace_only
    ]
    closure: dict[str, set[str]] = {}
    while queue:
        requirement = queue.pop()
        name = canonicalize_name(requirement.name)
        extras = set(requirement.extras) | {""}
        previous = closure.setdefault(name, set())
        new_extras = extras - previous
        if not new_extras:
            continue
        previous.update(new_extras)
        dist = source_dist[name]
        if dist.version not in requirement.specifier:
            raise SystemExit(f"Source dependency does not satisfy {requirement}: {dist.version}")
        for text in dist.requires or []:
            child = Requirement(text)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in new_extras
            ):
                queue.append(child)

    files: dict[str, dict[str, object]] = {}

    def include(source: Path, relative: Path, category: str) -> None:
        new_hash = digest(source)
        old_hash = digest(INSTALL / relative)
        if old_hash == new_hash:
            return
        target = payload / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files[relative.as_posix()] = {
            "path": relative.as_posix(),
            "category": category,
            "before_sha256": old_hash,
            "after_sha256": new_hash,
            "bytes": source.stat().st_size,
        }

    compiled = 0
    for directory in ("apps", "packages", "migrations"):
        for source in sorted((root / directory).rglob("*")):
            relative = source.relative_to(root)
            if "__pycache__" in relative.parts or relative.parts[:2] == ("apps", "web"):
                continue
            if relative.parts[:2] == ("apps", "mcp_server"):
                continue
            if source.is_file() and (source.suffix == ".py" or source.name == "script.py.mako"):
                if source.suffix == ".py":
                    compile(source.read_bytes(), str(source), "exec")
                    compiled += 1
                include(source, Path("app") / relative, "application")

    dependency_report = []
    skipped_record_paths = []
    for name in sorted(closure):
        dist = source_dist[name]
        old = installed_dist.get(name)
        if old and old["version"] != dist.version:
            raise SystemExit(f"Existing dependency version differs: {name}; needs explicit review")
        dependency_report.append(
            {
                "name": name,
                "version": dist.version,
                "installed_version": old["version"] if old else None,
                "action": "preserve" if old else "add-record-files",
            }
        )
        if old:
            continue
        if not dist.files:
            raise SystemExit(f"Missing dependency RECORD: {name}")
        for record in dist.files:
            if ".." in record.parts or "__pycache__" in record.parts or record.suffix == ".pyc":
                skipped_record_paths.append({"distribution": name, "path": str(record)})
                continue
            source = Path(dist.locate_file(record)).resolve()
            if not source.is_relative_to(source_site.resolve()):
                raise SystemExit(f"Dependency path escapes site-packages: {source}")
            if not source.is_file():
                raise SystemExit(f"Dependency RECORD file absent: {source}")
            include(source, Path("runtime/python/Lib/site-packages") / Path(record), name)

    release = root / "desktop/src-tauri/target/release/research-asset-hub-desktop.exe"
    if digest(release) != RELEASE_HASH:
        raise SystemExit("Release EXE hash differs from the reviewed build.")
    include(release, Path("research-asset-hub-desktop.exe"), "desktop")
    protected = {
        path.relative_to(INSTALL).as_posix(): digest(path)
        for path in sorted((INSTALL / "app/config").rglob("*"))
        if path.is_file()
    }
    git_status = subprocess.check_output(["git", "status", "--short"], cwd=root, encoding="utf-8")
    manifest = {
        "schema": 1,
        "created_utc": datetime.now(UTC).isoformat(),
        "installation": str(INSTALL),
        "workspace": str(root),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, encoding="utf-8"
        ).strip(),
        "git_status": git_status,
        "release_sha256": RELEASE_HASH,
        "installed_python": installed["python"],
        "source_python": sys.version,
        "compiled_python_files": compiled,
        "files": sorted(files.values(), key=lambda item: item["path"]),
        "protected_config": protected,
        "dependency_closure": dependency_report,
        "excluded_direct_dependencies": [
            "mcp (workspace-only bridge)",
            "pywin32 (workspace-only MCP launcher)",
        ],
        "excluded_application_trees": [
            "apps/web (embedded release frontend)",
            "apps/mcp_server (workspace-only bridge)",
        ],
        "excluded_dependency_record_paths": skipped_record_paths,
        "preserved": [
            "app/config",
            "runtime/python interpreter and existing packages",
            "runtime/postgres",
            "runtime/meilisearch.exe",
            "user data",
            "source files",
        ],
    }
    write_json(output / "canonical-update-manifest.json", manifest)
    shutil.copy2(root / "scripts/canonical-update-admin.ps1", output / "canonical-update-admin.ps1")
    print(
        json.dumps(
            {
                "package": str(output),
                "files": len(files),
                "bytes": sum(item["bytes"] for item in files.values()),
                "missing_dependencies": [
                    item for item in dependency_report if item["action"] != "preserve"
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
