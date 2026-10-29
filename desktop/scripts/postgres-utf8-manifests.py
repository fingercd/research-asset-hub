"""Set UTF-8 process manifests on copied PostgreSQL Windows executables.

Windows 10 1903+ supports a per-process activeCodePage manifest. PostgreSQL's
Windows tools otherwise receive legacy-encoded non-ASCII paths, which can make
UTF-8 initdb fail during post-bootstrap. Vendor inputs are verified before this
build-only transform; this report records each input and output SHA-256.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

ASSEMBLY = "urn:schemas-microsoft-com:asm.v1"
APPLICATION = "urn:schemas-microsoft-com:asm.v3"
CODE_PAGE = "http://schemas.microsoft.com/SMI/2019/WindowsSettings"
ET.register_namespace("", ASSEMBLY)
ET.register_namespace("asmv3", APPLICATION)
ET.register_namespace("windows2019", CODE_PAGE)


def utf8_manifest(data: bytes | None, name: str) -> bytes:
    if data:
        root = ET.fromstring(data.rstrip(b"\0"))
    else:
        root = ET.Element(f"{{{ASSEMBLY}}}assembly", {"manifestVersion": "1.0"})
        ET.SubElement(
            root,
            f"{{{ASSEMBLY}}}assemblyIdentity",
            {
                "type": "win32",
                "name": f"ResearchAssetHub.PostgreSQL.{name}",
                "version": "1.0.0.0",
                "processorArchitecture": "amd64",
            },
        )
    application = root.find(f"{{{APPLICATION}}}application")
    if application is None:
        application = ET.SubElement(root, f"{{{APPLICATION}}}application")
    settings = application.find(f"{{{APPLICATION}}}windowsSettings")
    if settings is None:
        settings = ET.SubElement(application, f"{{{APPLICATION}}}windowsSettings")
    page = settings.find(f"{{{CODE_PAGE}}}activeCodePage")
    if page is None:
        page = ET.SubElement(settings, f"{{{CODE_PAGE}}}activeCodePage")
    page.text = "UTF-8"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def patch_executable(path: Path) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel.FindResourceExW.argtypes = [
        wintypes.HMODULE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
    ]
    kernel.FindResourceExW.restype = wintypes.HRSRC
    kernel.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
    kernel.LoadResource.restype = wintypes.HGLOBAL
    kernel.LockResource.argtypes = [wintypes.HGLOBAL]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
    kernel.SizeofResource.restype = wintypes.DWORD
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMODULE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        wintypes.LPARAM,
    )
    kernel.EnumResourceLanguagesW.argtypes = [
        wintypes.HMODULE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        callback_type,
        wintypes.LPARAM,
    ]
    kernel.EnumResourceLanguagesW.restype = wintypes.BOOL
    module = kernel.LoadLibraryExW(str(path), None, 0x2 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    manifests: dict[int, bytes] = {}

    @callback_type
    def record_language(_module: int, _kind: int, _name: int, language: int, _data: int) -> bool:
        resource = kernel.FindResourceExW(module, 24, 1, language)
        loaded = kernel.LoadResource(module, resource)
        size = kernel.SizeofResource(module, resource)
        pointer = kernel.LockResource(loaded)
        manifests[language] = ctypes.string_at(pointer, size)
        return True

    try:
        result = kernel.EnumResourceLanguagesW(module, 24, 1, record_language, 0)
        if not result and ctypes.get_last_error() not in {1812, 1813, 1814, 1815}:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.FreeLibrary(module)
    if not manifests:
        manifests[0] = b""
    kernel.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.UpdateResourceW.restype = wintypes.BOOL
    kernel.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel.EndUpdateResourceW.restype = wintypes.BOOL
    handle = kernel.BeginUpdateResourceW(str(path), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    committed = False
    try:
        for language, data in manifests.items():
            content = utf8_manifest(data or None, path.stem)
            buffer = ctypes.create_string_buffer(content)
            if not kernel.UpdateResourceW(handle, 24, 1, language, buffer, len(content)):
                raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.EndUpdateResourceW(handle, False):
            raise ctypes.WinError(ctypes.get_last_error())
        committed = True
    finally:
        if not committed:
            kernel.EndUpdateResourceW(handle, True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("PostgreSQL Windows manifest transform requires a Windows builder")
    root = args.postgres_directory.resolve()
    paths = sorted((root / "bin").glob("*.exe"))
    if not any(path.name == "initdb.exe" for path in paths):
        raise ValueError("PostgreSQL binary input is incomplete")
    records = []
    for path in paths:
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        patch_executable(path)
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "input_sha256": before,
                "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    args.report.write_text(
        json.dumps(
            {
                "schema": "research-asset-hub.postgres-manifest-transform/v1",
                "minimum_windows_build": 18362,
                "active_code_page": "UTF-8",
                "files": records,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"UTF-8 process manifests applied to {len(records)} copied PostgreSQL tools")


if __name__ == "__main__":
    main()
