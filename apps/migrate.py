"""Skip Alembic imports when the database already has every packaged head."""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path


def migration_heads(directory: Path) -> set[str] | None:
    """Read literal revision metadata without importing SQLAlchemy or migrations."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in directory.glob("*.py"):
        if path.name == "__init__.py":
            continue
        metadata: dict[str, object] = {}
        for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if node.value is None:
                continue
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else ([node.target] if isinstance(node, ast.AnnAssign) else [])
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                    "depends_on",
                }:
                    try:
                        metadata[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        return None  # Let Alembic evaluate non-literal metadata.
        revision = metadata.get("revision")
        if not isinstance(revision, str) or metadata.get("depends_on") is not None:
            return None
        revisions.add(revision)
        parent = metadata.get("down_revision")
        if isinstance(parent, str):
            parents.add(parent)
        elif isinstance(parent, tuple) and all(isinstance(item, str) for item in parent):
            parents.update(parent)
        elif parent is not None:
            return None
    return revisions - parents if parents <= revisions else None


def is_current(root: Path, database_url: str, *, wait_seconds: float = 0) -> bool:
    heads = migration_heads(root / "migrations" / "versions")
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        return False
    import psycopg

    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://", 1),
                connect_timeout=1,
            ) as connection:
                if not heads:
                    return False
                rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
                return {row[0] for row in rows} == heads
        except psycopg.OperationalError:
            # The desktop imports its application while PostgreSQL starts. Wait
            # only on that path; standalone checks preserve immediate fallback.
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        except psycopg.Error:
            # Missing schema: Alembic owns migration and diagnostic output.
            return False


def main(*, wait_seconds: float = 0) -> None:
    root = Path(__file__).resolve().parents[1]
    url = os.environ.get("DATABASE_URL") or os.environ.get("HUB_DATABASE_URL", "")
    if is_current(root, url, wait_seconds=wait_seconds):
        print("Database schema is current; skipping Alembic startup.", flush=True)
        return
    from alembic.config import main as alembic_main

    alembic_main(argv=["-c", str(root / "migrations" / "alembic.ini"), "upgrade", "head"])


if __name__ == "__main__":
    main()
