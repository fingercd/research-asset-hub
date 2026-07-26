"""Build a deterministic canonical sidecar from one explicitly selected source profile."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from packages.ingestion.canonical_snapshot import (
    build_canonical_snapshot,
    write_canonical_snapshot,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Join only the fixed catalogs declared by a source profile; "
            "the builder never recursively scans source or work directories."
        )
    )
    parser.add_argument("--profile", choices=("vln", "game_rl", "wsvad"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = args.profile
    snapshot = build_canonical_snapshot(
        profile=profile,
        source_root=args.root,
        source_id=args.source_id,
        domain_slug=args.domain,
    )
    write_canonical_snapshot(
        snapshot,
        source_root=args.root,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "status": "written",
                "profile": profile,
                "source_id": args.source_id,
                "records": len(snapshot.items),
                "representations": sum(
                    len(item.representations) for item in snapshot.items
                ),
                "relations": sum(len(item.relations) for item in snapshot.items),
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
