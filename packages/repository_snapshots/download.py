from __future__ import annotations

import hashlib
import re
import stat
import struct
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, cast
from urllib.parse import quote, urlparse

import httpx

ARCHIVE_LIMIT = 100 * 1024 * 1024
TREE_LIMIT = 500 * 1024 * 1024
FILE_LIMIT = 10 * 1024 * 1024
ENTRY_LIMIT = 20_000
SNAPSHOT_SCOPE = (
    "Public source archive only; no Git history, submodules, LFS payloads, weights or datasets. "
    "Code is never executed."
)
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "weights",
    "checkpoints",
    "datasets",
}
_EXCLUDED_EXTENSIONS = {
    ".pt",
    ".pth",
    ".ckpt",
    ".safetensors",
    ".bin",
    ".h5",
    ".hdf5",
    ".onnx",
    ".npy",
    ".npz",
}


class SnapshotError(ValueError):
    """A safe public archive cannot be materialized."""


class SnapshotCancelled(SnapshotError):
    pass


@dataclass(frozen=True)
class PublicRepository:
    canonical_url: str
    host: str
    project: str

    def commit_url(self, revision: str | None) -> str:
        ref = quote(revision or "HEAD", safe="")
        if self.host == "github.com":
            return f"https://api.github.com/repos/{self.project}/commits/{ref}"
        project = quote(self.project, safe="")
        return f"https://gitlab.com/api/v4/projects/{project}/repository/commits/{ref}"

    def archive_url(self, commit: str) -> str:
        if self.host == "github.com":
            return f"https://codeload.github.com/{self.project}/zip/{commit}"
        project = quote(self.project, safe="")
        return (
            f"https://gitlab.com/api/v4/projects/{project}/repository/archive.tar.gz?sha={commit}"
        )


def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError as error:
        raise SnapshotError("Code URL is invalid.") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port
    ):
        raise SnapshotError(
            "Code URLs must be public HTTPS URLs without credentials or custom ports."
        )
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    # Query tokens and fragments are never persisted as repository identities.
    return f"https://{parsed.hostname.casefold()}{path}"


def public_repository(value: str) -> PublicRepository:
    canonical = canonical_url(value)
    parsed = urlparse(canonical)
    components = parsed.path.strip("/").split("/")
    host = parsed.hostname or ""
    if host not in {"github.com", "gitlab.com"}:
        raise SnapshotError(
            "Snapshots support public github.com and gitlab.com repositories only; "
            "this URL can be linked online."
        )
    if (
        len(components) < 2
        or (host == "github.com" and len(components) != 2)
        or any(not _COMPONENT.fullmatch(part) or part in {".", "..", "-"} for part in components)
    ):
        raise SnapshotError("Enter the repository root URL, not a file, branch or project page.")
    return PublicRepository(canonical, host, "/".join(components))


def resolve_commit(client: httpx.Client, repository: PublicRepository, revision: str | None) -> str:
    if revision is not None and (not revision.strip() or len(revision) > 255 or "\0" in revision):
        raise SnapshotError("Revision must contain between 1 and 255 characters.")
    response = client.get(repository.commit_url(revision), follow_redirects=False)
    if response.status_code != 200:
        raise SnapshotError(
            f"Public commit lookup failed with HTTP {response.status_code}. "
            "Only public repositories are supported."
        )
    try:
        document = response.json()
        commit = document.get("sha") if repository.host == "github.com" else document.get("id")
    except (ValueError, AttributeError) as error:
        raise SnapshotError("Commit lookup did not return a valid public commit.") from error
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise SnapshotError("Commit lookup did not return a full 40-character commit SHA.")
    if revision and _SHA.fullmatch(revision) and revision.casefold() != commit.casefold():
        raise SnapshotError("Commit lookup does not match the requested fixed commit SHA.")
    return commit.lower()


def download_archive(
    client: httpx.Client,
    repository: PublicRepository,
    commit: str,
    destination: Path,
    check_cancel: Callable[[], None],
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    # Hosts and paths are constructed above; redirect destinations are never followed.
    with client.stream("GET", repository.archive_url(commit), follow_redirects=False) as response:
        if response.status_code != 200:
            raise SnapshotError(
                f"Public repository archive failed with HTTP {response.status_code}."
            )
        length = response.headers.get("content-length")
        if length and length.isdecimal() and int(length) > ARCHIVE_LIMIT:
            raise SnapshotError(f"Archive exceeds {ARCHIVE_LIMIT} byte download limit.")
        with destination.open("xb") as handle:
            for chunk in response.iter_bytes(64 * 1024):
                check_cancel()
                size += len(chunk)
                if size > ARCHIVE_LIMIT:
                    raise SnapshotError(f"Archive exceeds {ARCHIVE_LIMIT} byte download limit.")
                handle.write(chunk)
                digest.update(chunk)
    return digest.hexdigest(), size


def _member_path(name: str) -> PurePosixPath:
    # Reject Windows device/ADS paths as well as POSIX traversal before writing.
    if "\\" in name or "\0" in name or ":" in name or name.startswith("/"):
        raise SnapshotError("Archive contains an unsafe path.")
    parts = name.rstrip("/").split("/")
    if any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
        for part in parts
    ):
        raise SnapshotError("Archive contains an unsafe path.")
    return PurePosixPath(*parts)


def extract_archive(
    archive: Path, destination: Path, check_cancel: Callable[[], None]
) -> dict[str, object]:
    destination.mkdir()
    count = 0
    total = 0
    declared_total = 0
    skipped: dict[str, int] = {}
    tree_digest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    archive_root: str | None = None
    written: set[str] = set()

    def write_member(
        name: str, size: int, directory: bool, link: bool, opener: Callable[[], IO[bytes]]
    ) -> None:
        nonlocal count, total, declared_total, archive_root
        check_cancel()
        count += 1
        if count > ENTRY_LIMIT:
            raise SnapshotError(f"Archive exceeds {ENTRY_LIMIT} entry limit.")
        path = _member_path(name)
        if link:
            raise SnapshotError("Archive symlinks, hard links and special files are not supported.")
        declared_total += size
        if declared_total > TREE_LIMIT:
            raise SnapshotError(
                f"Archive exceeds {TREE_LIMIT} byte extracted tree limit, including skipped files."
            )
        if archive_root is None:
            archive_root = path.parts[0]
        if path.parts[0] != archive_root:
            raise SnapshotError("Archive must contain one repository root directory.")
        relative = PurePosixPath(*path.parts[1:])
        if len(path.parts) == 1:
            if not directory:
                raise SnapshotError("Archive has no repository root directory.")
            return
        if (
            any(part.casefold() in _EXCLUDED_DIRS for part in relative.parts)
            or relative.suffix.casefold() in _EXCLUDED_EXTENSIONS
        ):
            skipped["excluded_weights_datasets_dependencies"] = (
                skipped.get("excluded_weights_datasets_dependencies", 0) + 1
            )
            return
        if relative.as_posix() == ".hub-snapshot.json":
            raise SnapshotError("Archive contains a reserved snapshot evidence filename.")
        if size > FILE_LIMIT:
            skipped["file_size_limit"] = skipped.get("file_size_limit", 0) + 1
            return
        if directory:
            destination.joinpath(*relative.parts).mkdir(parents=True, exist_ok=True)
            return
        key = relative.as_posix().casefold()
        if key in written:
            raise SnapshotError("Archive contains duplicate or case-colliding file paths.")
        written.add(key)
        total += size
        if total > TREE_LIMIT:
            raise SnapshotError(f"Archive exceeds {TREE_LIMIT} byte extracted tree limit.")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_digest = hashlib.sha256()
        actual = 0
        with opener() as source, target.open("xb") as output:
            while chunk := source.read(64 * 1024):
                check_cancel()
                actual += len(chunk)
                if actual > size or actual > FILE_LIMIT:
                    raise SnapshotError("Archive member exceeds its declared size or file limit.")
                output.write(chunk)
                file_digest.update(chunk)
        if actual != size:
            raise SnapshotError("Archive member is truncated.")
        file_hashes[relative.as_posix()] = file_digest.hexdigest()

    if zipfile.is_zipfile(archive):
        # Enforce the ZIP entry bound before ZipFile allocates its complete member list.
        with archive.open("rb") as handle:
            size = archive.stat().st_size
            handle.seek(max(0, size - 65_557))
            footer = handle.read()
        end = footer.rfind(b"PK\x05\x06")
        if end < 0 or len(footer) - end < 22:
            raise SnapshotError("Archive has an invalid ZIP directory.")
        entry_count = struct.unpack_from("<H", footer, end + 10)[0]
        if entry_count > ENTRY_LIMIT:
            raise SnapshotError(f"Archive exceeds {ENTRY_LIMIT} entry limit.")
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)

                def open_zip_member(zip_member: zipfile.ZipInfo = member) -> IO[bytes]:
                    return zipped.open(zip_member)

                write_member(
                    member.orig_filename,
                    member.file_size,
                    member.is_dir(),
                    kind not in {0, stat.S_IFREG, stat.S_IFDIR},
                    open_zip_member,
                )
    else:
        with tarfile.open(archive, mode="r|gz") as tarred:
            for tar_member in tarred:

                def open_tar_member(entry: tarfile.TarInfo = tar_member) -> IO[bytes]:
                    return cast(IO[bytes], tarred.extractfile(entry))

                write_member(
                    tar_member.name,
                    tar_member.size,
                    tar_member.isdir(),
                    not (tar_member.isdir() or tar_member.isfile()),
                    open_tar_member,
                )
    if not written:
        raise SnapshotError("Archive contains no eligible source files.")
    for name, digest in sorted(file_hashes.items()):
        tree_digest.update(f"{name}\0{digest}\n".encode())
    return {
        "file_count": len(written),
        "entry_count": count,
        "size_bytes": total,
        "content_tree_digest": tree_digest.hexdigest(),
        "skipped": skipped,
        "scope": SNAPSHOT_SCOPE,
        "limits": {
            "archive_bytes": ARCHIVE_LIMIT,
            "tree_bytes": TREE_LIMIT,
            "file_bytes": FILE_LIMIT,
            "entries": ENTRY_LIMIT,
        },
    }
