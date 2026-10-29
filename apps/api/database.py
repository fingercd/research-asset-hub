from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.config import PROJECT_ROOT, ApiSettings

if TYPE_CHECKING:
    from alembic.script import ScriptDirectory

REQUIRED_TABLES = frozenset(
    {
        "assets",
        "representations",
        "annotations",
        "reading_states",
        "relations",
        "audit_events",
        "jobs",
        "outbox_events",
        "document_chunks",
        "chat_sessions",
        "sync_conflicts",
    }
)


class DatabaseUnavailable(RuntimeError):
    """Raised when the operational database cannot safely serve API requests."""


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    status: str
    detail: str
    revision: str | None = None


def sqlalchemy_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def create_database_engine(settings: ApiSettings) -> Engine:
    url = make_url(sqlalchemy_url(settings.database_url))
    options: dict[str, object] = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.drivername.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if url.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
    elif url.drivername.startswith("postgresql"):
        options["connect_args"] = {
            "connect_timeout": max(1, round(settings.database_connect_timeout_seconds))
        }
    return create_engine(url, **options)


class DatabaseManager:
    def __init__(
        self,
        settings: ApiSettings,
        *,
        engine: Engine | None = None,
        require_migrations: bool | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine or create_database_engine(settings)
        self.require_migrations = (
            settings.database_require_migrations
            if require_migrations is None
            else require_migrations
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        self._readiness: DatabaseReadiness | None = None
        self._checked_at = 0.0
        self._lock = Lock()
        self._script_directory_cached: ScriptDirectory | None = None

    def _script_directory(self) -> ScriptDirectory:
        if self._script_directory_cached is None:
            from alembic.config import Config as AlembicConfig
            from alembic.script import ScriptDirectory

            config = AlembicConfig(str(PROJECT_ROOT / "migrations" / "alembic.ini"))
            self._script_directory_cached = ScriptDirectory.from_config(config)
        return self._script_directory_cached

    def _revision_satisfies_requirement(self, revision: str | None) -> bool:
        """数据库修订必须是 required 修订本身或其迁移链上的后代。"""
        if revision is None:
            return False
        required = self.settings.database_required_revision
        if revision == required:
            return True
        from alembic.script.revision import RevisionError
        from alembic.util.exc import CommandError

        try:
            scripts = self._script_directory()
        except (CommandError, OSError):
            return False
        try:
            chain = [
                script.revision
                for script in scripts.revision_map.iterate_revisions(
                    revision, required, inclusive=True
                )
            ]
        except RevisionError:
            # 未知修订，或 required 不在 DB 修订的祖先链上（即 DB 落后）。
            return False
        return required in chain

    def check(self, *, force: bool = False) -> DatabaseReadiness:
        with self._lock:
            if not force and self._readiness is not None and monotonic() - self._checked_at < 2.0:
                return self._readiness
            try:
                with self.engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                    tables = set(inspect(connection).get_table_names())
                    missing = sorted(REQUIRED_TABLES - tables)
                    if missing:
                        readiness = DatabaseReadiness(
                            "unmigrated",
                            f"Database schema is missing required tables: {', '.join(missing)}.",
                        )
                    else:
                        revision: str | None = None
                        if "alembic_version" in tables:
                            revision = connection.execute(
                                text("SELECT version_num FROM alembic_version")
                            ).scalar_one_or_none()
                        if self.require_migrations and not self._revision_satisfies_requirement(
                            revision
                        ):
                            readiness = DatabaseReadiness(
                                "unmigrated",
                                (
                                    "Database revision is "
                                    f"{revision or 'unknown'}; expected "
                                    f"{self.settings.database_required_revision}."
                                ),
                                revision,
                            )
                        else:
                            readiness = DatabaseReadiness(
                                "ready",
                                "Database schema is ready.",
                                revision,
                            )
            except (SQLAlchemyError, OSError) as error:
                readiness = DatabaseReadiness(
                    "unavailable",
                    f"{type(error).__name__}: database connection or inspection failed.",
                )
            self._readiness = readiness
            self._checked_at = monotonic()
            return readiness

    def require_ready(self) -> None:
        readiness = self.check()
        if readiness.status != "ready":
            raise DatabaseUnavailable(readiness.detail)

    @contextmanager
    def session(self) -> Iterator[Session]:
        self.require_ready()
        with self.session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def dispose(self) -> None:
        self.engine.dispose()
