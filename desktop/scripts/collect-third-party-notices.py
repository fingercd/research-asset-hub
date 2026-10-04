"""Collect installed dependency license texts without embedding host paths.

Inputs are inventories produced from the clean Python environment, filtered
Cargo metadata and ``pnpm licenses list --prod --json``. Supplemental notices
contain reviewed upstream texts for native binaries and crates whose wheel or
crate archive omits its license. This collector does not choose licenses or
claim that an inventory alone is a complete legal review.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
from typing import Any

LICENSE_WORDS = ("license", "licence", "copying", "notice", "copyright")


def is_license(path: Path) -> bool:
    return any(word in path.name.lower() for word in LICENSE_WORDS)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def collect(args: argparse.Namespace) -> None:
    sections = [
        "Research Asset Hub third-party notices",
        "The application source is MIT licensed. Dependencies retain their own "
        "licenses. The following installed-package and upstream notices accompany "
        "the candidate runtime. No third-party package is relicensed by this file.",
    ]
    missing: list[str] = []
    inventory: list[dict[str, Any]] = []

    def add(name: str, version: str, license_name: str, paths: list[Path], url: str = "") -> None:
        if not paths:
            supplemental = args.supplemental / f"{name}-{version}.txt"
            if supplemental.is_file():
                paths = [supplemental]
            else:
                missing.append(f"{name} {version} ({license_name})")
        sections.append(f"\n{'=' * 72}\n{name} {version}\nLicense: {license_name}\n{url}")
        for path in sorted(set(paths)):
            sections.append(
                f"\n--- {path.name} ---\n" + path.read_text(encoding="utf-8", errors="replace")
            )
        inventory.append(
            {
                "name": name,
                "version": version,
                "license": license_name,
                "license_files": [path.name for path in paths],
                "source": url,
            }
        )

    for dist in sorted(
        importlib.metadata.distributions(path=[str(args.python_site)]),
        key=lambda item: item.metadata["Name"].lower(),
    ):
        metadata = dist.metadata
        paths = [
            args.python_site / str(file) for file in dist.files or () if is_license(Path(str(file)))
        ]
        # PDFium ships its wrapper licenses as SPDX-named texts next to the
        # complete native PDFium third-party notice.
        if metadata["Name"] == "pypdfium2":
            paths.extend((args.python_site / f"pypdfium2-{dist.version}.dist-info").glob("*.txt"))
        add(
            metadata["Name"],
            dist.version,
            metadata.get("License-Expression")
            or metadata.get("License")
            or ", ".join(
                value
                for value in metadata.get_all("Classifier", [])
                if value.startswith("License ::")
            ),
            paths,
            "; ".join(metadata.get_all("Project-URL", [])),
        )

    cargo = read_json(args.cargo_metadata)
    for package in sorted(cargo["packages"], key=lambda item: (item["name"], item["version"])):
        if not package["source"]:
            continue
        root = Path(package["manifest_path"]).parent
        paths = [file for file in root.iterdir() if file.is_file() and is_license(file)]
        if package.get("license_file"):
            paths.append(root / package["license_file"])
        add(
            package["name"],
            package["version"],
            package.get("license") or "See license file",
            paths,
            package.get("repository") or "",
        )

    for license_name, packages in sorted(read_json(args.npm_inventory).items()):
        for package in sorted(packages, key=lambda item: item["name"]):
            for version in package["versions"]:
                root = next(
                    Path(value)
                    for value in package["paths"]
                    if read_json(Path(value) / "package.json")["version"] == version
                )
                paths = [file for file in root.iterdir() if file.is_file() and is_license(file)]
                add(
                    package["name"].replace("/", "__"),
                    version,
                    license_name,
                    paths,
                    package.get("homepage") or "",
                )

    # Native notices are reviewed separately; full license texts and provenance
    # URLs are included in each supplied text, without local installation paths.
    for path in sorted(args.supplemental.glob("native-*.txt")):
        sections.append(f"\n{'=' * 72}\n" + path.read_text(encoding="utf-8"))
    if missing:
        raise ValueError(
            "License texts missing; supply reviewed supplemental files: " + "; ".join(missing)
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(sections), encoding="utf-8")
    args.output.with_suffix(".inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Collected {len(inventory)} dependency notices; {args.output.stat().st_size} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-site", type=Path, required=True)
    parser.add_argument("--cargo-metadata", type=Path, required=True)
    parser.add_argument("--npm-inventory", type=Path, required=True)
    parser.add_argument("--supplemental", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    collect(parser.parse_args())


if __name__ == "__main__":
    main()
