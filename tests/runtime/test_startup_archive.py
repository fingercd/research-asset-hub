import subprocess
import sys
import zipfile
from pathlib import Path

from scripts.build_startup_archive import build_archive


def test_archive_imports_bytecode_and_leaves_native_packages_on_disk(tmp_path: Path) -> None:
    stdlib = tmp_path / "stdlib"
    site = tmp_path / "site"
    stdlib.mkdir()
    site.mkdir()
    (stdlib / "startup_probe.py").write_text("answer: int = 42\n", encoding="utf-8")
    tests = stdlib / "tests"
    tests.mkdir()
    (tests / "ignored.py").write_text("raise RuntimeError()\n", encoding="utf-8")
    (site / "typing_extensions.py").write_text("marker = 'packed'\n", encoding="utf-8")
    native = site / "fastapi"
    native.mkdir()
    (native / "__init__.py").write_text("marker = 'native'\n", encoding="utf-8")
    (native / "engine.pyd").touch()
    archive = tmp_path / "startup.zip"
    assert build_archive(stdlib, site, archive) == 2
    with zipfile.ZipFile(archive) as packed:
        assert set(packed.namelist()) == {"startup_probe.pyc", "typing_extensions.pyc"}
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import startup_probe; "
            "assert startup_probe.answer == 42; "
            "assert startup_probe.__annotations__['answer'] is int",
            str(archive),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr


def test_native_passthrough_preserves_docstring_and_real_package_path(tmp_path: Path) -> None:
    stdlib = tmp_path / "stdlib"
    site = tmp_path / "site"
    package = site / "sqlalchemy"
    stdlib.mkdir()
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Package documentation."""\nfrom __future__ import annotations\nvalue: int = 7\n',
        encoding="utf-8",
    )
    extension_package = package / "cyextension"
    extension_package.mkdir()
    (extension_package / "__init__.py").write_text("", encoding="utf-8")
    (extension_package / "engine.pyd").touch()
    archive = tmp_path / "startup.zip"
    assert build_archive(stdlib, site, archive) == 2
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys, pathlib; sys.path[:0] = sys.argv[1:3]; import sqlalchemy; "
            "assert sqlalchemy.__doc__ == 'Package documentation.'; "
            "assert sqlalchemy.value == 7; "
            "assert sqlalchemy.__annotations__['value'] == 'int'; "
            "assert str(pathlib.Path(sys.argv[2]) / 'sqlalchemy') in sqlalchemy.__path__; "
            "import sqlalchemy.cyextension as native; "
            "native_path = pathlib.Path(sys.argv[2]) / 'sqlalchemy' / 'cyextension'; "
            "assert str(native_path) in native.__path__",
            str(archive),
            str(site),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
