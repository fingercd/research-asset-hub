"""Build the desktop's pure-Python startup archive without changing dependencies."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import marshal
import os
import struct
import sys
import sysconfig
import zipfile
from collections.abc import Iterator
from pathlib import Path

PURE_STARTUP_PACKAGES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "click",
    "anyio",
    "httpx",
    "httpcore",
    "pydantic",
    "pydantic_settings",
    "psycopg",
    "typing_extensions.py",
    "sniffio",
    "h11",
    "annotated_types",
    "typing_inspection",
    "idna",
    "sqlalchemy",
    "yaml",
)
NATIVE_PASSTHROUGH_PACKAGES = {"sqlalchemy", "yaml"}
EXCLUDED_DIRECTORIES = {
    "__pycache__",
    "site-packages",
    "test",
    "tests",
    "idlelib",
    "tkinter",
    "turtledemo",
    "ensurepip",
    "venv",
}


def python_files(root: Path) -> Iterator[Path]:
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name not in EXCLUDED_DIRECTORIES)
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(directory) / name


def build_archive(stdlib: Path, site_packages: Path, output: Path) -> int:
    """Keep native extensions and resource-bearing packages in their real folders."""
    modules = [(path, path.relative_to(stdlib)) for path in python_files(stdlib)]
    native_package_paths: set[Path] = set()
    for name in PURE_STARTUP_PACKAGES:
        root = site_packages / name
        if not root.exists():
            continue
        if root.is_file():
            files = [root]
        else:
            native_files = [*root.rglob("*.pyd"), *root.rglob("*.so")]
            # Never hide a package's native extension behind zipimport's __path__.
            if name not in NATIVE_PASSTHROUGH_PACKAGES and native_files:
                continue
            for native in native_files:
                parent = native.parent
                while parent != site_packages:
                    native_package_paths.add(parent.relative_to(site_packages) / "__init__.py")
                    parent = parent.parent
            files = list(python_files(root))
        modules.extend((path, path.relative_to(site_packages)) for path in files)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for source_path, relative in modules:
                source = source_path.read_bytes()
                header = importlib.util.MAGIC_NUMBER + struct.pack(
                    "<III", 0, int(source_path.stat().st_mtime), len(source)
                )
                if relative in native_package_paths:
                    tree = ast.parse(source, filename=str(source_path))
                    position = 0
                    if (
                        tree.body
                        and isinstance(tree.body[0], ast.Expr)
                        and isinstance(tree.body[0].value, ast.Constant)
                        and isinstance(tree.body[0].value.value, str)
                    ):
                        position = 1
                    while position < len(tree.body):
                        statement = tree.body[position]
                        if (
                            not isinstance(statement, ast.ImportFrom)
                            or statement.module != "__future__"
                        ):
                            break
                        position += 1
                    # Standard namespace extension keeps the original directory
                    # available for .pyd modules while Python code comes from ZIP.
                    tree.body[position:position] = ast.parse(
                        "from pkgutil import extend_path as _desktop_extend_path\n"
                        "__path__ = _desktop_extend_path(__path__, __name__)\n"
                        "del _desktop_extend_path\n"
                    ).body
                    code = compile(
                        ast.fix_missing_locations(tree),
                        str(source_path),
                        "exec",
                        dont_inherit=True,
                        optimize=0,
                    )
                else:
                    code = compile(source, str(source_path), "exec", dont_inherit=True, optimize=0)
                archive.writestr(
                    relative.with_suffix(".pyc").as_posix(), header + marshal.dumps(code)
                )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(modules)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.app_dir / f"python-startup-{sys.implementation.cache_tag}.zip"
    count = build_archive(
        Path(sysconfig.get_path("stdlib")), Path(sysconfig.get_path("purelib")), output
    )
    print(json.dumps({"archive": str(output), "modules": count, "bytes": output.stat().st_size}))


if __name__ == "__main__":
    main()
