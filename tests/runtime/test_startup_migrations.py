from pathlib import Path
from unittest.mock import MagicMock, patch

from apps.migrate import is_current, main, migration_heads


def write_revision(directory: Path, name: str, parent: str | tuple[str, ...] | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(
        f"revision: str = {name!r}\ndown_revision = {parent!r}\n", encoding="utf-8"
    )


def test_heads_support_branches_and_merge(tmp_path: Path) -> None:
    write_revision(tmp_path, "a", None)
    write_revision(tmp_path, "b", "a")
    write_revision(tmp_path, "c", "a")
    assert migration_heads(tmp_path) == {"b", "c"}
    write_revision(tmp_path, "d", ("b", "c"))
    assert migration_heads(tmp_path) == {"d"}


def test_dynamic_metadata_and_missing_parent_do_not_skip(tmp_path: Path) -> None:
    write_revision(tmp_path, "a", "missing")
    assert migration_heads(tmp_path) is None
    (tmp_path / "a.py").write_text("revision = build_revision()", encoding="utf-8")
    assert migration_heads(tmp_path) is None


def test_only_exact_database_heads_skip_migration(tmp_path: Path) -> None:
    write_revision(tmp_path / "migrations" / "versions", "a", None)
    connect = MagicMock()
    query = connect.return_value.__enter__.return_value.execute
    with patch("psycopg.connect", connect):
        for revisions, expected in [
            ([("a",)], True),
            ([], False),
            ([("older",)], False),
            ([("a",), ("unknown",)], False),
        ]:
            query.return_value.fetchall.return_value = revisions
            assert is_current(tmp_path, "postgresql://localhost/hub") is expected
    assert query.call_args.args == ("SELECT version_num FROM alembic_version",)


def test_missing_database_falls_back_to_alembic(tmp_path: Path) -> None:
    import psycopg

    write_revision(tmp_path / "migrations" / "versions", "a", None)
    with patch("psycopg.connect", side_effect=psycopg.OperationalError("offline")):
        assert not is_current(tmp_path, "postgresql://localhost/hub")
    with patch("apps.migrate.is_current", return_value=False), patch("alembic.config.main") as run:
        main()
    assert run.call_args.kwargs["argv"][-2:] == ["upgrade", "head"]


def test_current_database_does_not_run_alembic() -> None:
    with patch("apps.migrate.is_current", return_value=True), patch("alembic.config.main") as run:
        main()
    run.assert_not_called()


def test_desktop_waits_for_database_before_checking_schema(tmp_path: Path) -> None:
    import psycopg

    write_revision(tmp_path / "migrations" / "versions", "a", None)
    connection = MagicMock()
    connection.__enter__.return_value.execute.return_value.fetchall.return_value = [("a",)]
    with (
        patch("psycopg.connect", side_effect=[psycopg.OperationalError("starting"), connection]),
        patch("apps.migrate.time.sleep") as sleep,
    ):
        assert is_current(tmp_path, "postgresql://localhost/hub", wait_seconds=1)
    sleep.assert_called_once_with(0.1)
