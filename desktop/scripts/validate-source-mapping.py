"""Check copied configuration preserves every declared immutable source root."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.asset_gateway.registry import SourceRegistry  # noqa: E402


def check_mapping(reference: Path, candidate: Path, workspace_root: Path) -> None:
    previous = os.environ.pop("HUB_HOST_WORKSPACE_ROOT", None)
    try:
        original = SourceRegistry.load(reference)
        expected = {(item.source_id, item.root_key): item.path for item in original.describe()}
        os.environ["HUB_HOST_WORKSPACE_ROOT"] = str(workspace_root.resolve())
        copied = SourceRegistry.load(candidate)
        actual = {(item.source_id, item.root_key): item.path for item in copied.describe()}
        if actual != expected:
            raise ValueError("Copied sources configuration changes registered root mappings")
    finally:
        if previous is None:
            os.environ.pop("HUB_HOST_WORKSPACE_ROOT", None)
        else:
            os.environ["HUB_HOST_WORKSPACE_ROOT"] = previous


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    check_mapping(args.reference, args.candidate, args.workspace_root)
    print("Immutable source root mappings verified")


if __name__ == "__main__":
    main()
