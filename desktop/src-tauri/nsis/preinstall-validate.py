"""Validate legacy/copy roots using the OLD installed registry and interpreter."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    installed_app = args.install_dir / "app"
    sys.path.insert(0, str(installed_app))
    from apps.asset_gateway.registry import SourceRegistry

    reference = SourceRegistry.load(args.reference)
    roots = reference.describe()
    resource_roots = [(args.install_dir / name).resolve() for name in ("app", "runtime")]
    preference = Path(os.environ.get("LOCALAPPDATA", "")) / "ResearchAssetHub/data-directory.txt"
    selected = preference.read_text(encoding="utf-8-sig").strip() if preference.is_file() else None
    data = Path(
        os.environ.get("HUB_DATA_DIR")
        or selected
        or (Path.home() / "Documents" / "ResearchAssetHub")
    ).resolve()
    if any(
        path.is_relative_to(resource)
        for path in (data, (data / "config").resolve())
        for resource in resource_roots
    ):
        raise ValueError("Resolved user data/config lies inside installation resources")
    for root in roots:
        resolved = root.path.resolve()
        if any(
            resolved.is_relative_to(resource) or resource.is_relative_to(resolved)
            for resource in resource_roots
        ):
            raise ValueError("Registered originals overlap installation resources; upgrade aborted")
    if args.candidate is not None:
        workspace_value = reference.root_status()["workspace_root"]
        if workspace_value is None:
            raise ValueError("Legacy registry does not report its workspace root")
        workspace = str(workspace_value)
        os.environ["HUB_HOST_WORKSPACE_ROOT"] = workspace
        candidate = SourceRegistry.load(args.candidate)
        expected = {(root.source_id, root.root_key): root.path for root in roots}
        actual = {(root.source_id, root.root_key): root.path for root in candidate.describe()}
        if expected != actual:
            raise ValueError("Copied legacy configuration changes registered source root mappings")
        (args.candidate.parent / "workspace-root.txt").write_text(workspace, encoding="utf-8")
    print("Legacy source roots verified; installation resource trees may be preserved")


if __name__ == "__main__":
    main()
