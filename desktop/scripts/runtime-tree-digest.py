"""Print a portable SHA-256 of relative file paths, sizes and contents."""

import hashlib
import sys
from pathlib import Path


def tree_digest(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    files = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in files:
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Runtime tree link escapes root: {path}")
        if not path.is_file():
            continue
        content = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                content.update(block)
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content.hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


if __name__ == "__main__":
    print(tree_digest(Path(sys.argv[1])))
