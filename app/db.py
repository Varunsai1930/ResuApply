"""Database engine, sessions and column types.

Synchronous SQLAlchemy 2.x on SQLite. Nested content is stored as JSON and validated
with Pydantic on the way in and out; identifiers, relationships, statuses and timestamps
use ordinary columns. The schema is managed by Alembic migrations in ``app/migrations``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Request
from pydantic import TypeAdapter
from sqlalchemy import JSON, DateTime, Engine, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger("resuapply.db")


# SQLite stores integers in 8 bytes. A larger number in a URL or form can't be any row's ID, and
# passing it to a query raises OverflowError (a 500) instead of finding nothing.
MAX_ID = 2**63 - 1


def is_db_id(value: object) -> bool:
    """Whether a value from a request could be a row ID."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_ID


def get_by_id(session: Session, model: type, value: object):
    """``session.get`` for an ID taken from a request: an impossible ID is simply not found."""
    return session.get(model, value) if is_db_id(value) else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes. SQLite stores them naive; this restores the zone."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime needs a timezone-aware datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)


class PydanticJSON(TypeDecorator):
    """A JSON column validated against a Pydantic type on write and read.

    Values are treated as immutable: assign a new object to change the column, never
    mutate the loaded value in place (SQLAlchemy would not notice the change).
    """

    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_type: Any, read_upgrade: Callable[[Any], Any] | None = None):
        """``read_upgrade`` adapts stored JSON from an older shape before it is validated.

        It runs only on values read from the database, so objects built in code are still
        validated strictly.
        """
        super().__init__()
        self.pydantic_type = pydantic_type
        self.read_upgrade = read_upgrade
        self.adapter = TypeAdapter(pydantic_type)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return self.adapter.dump_python(
            self.adapter.validate_python(value), mode="json", by_alias=True,
            context={"preserve_stored_snapshot_profile": True},
        )

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if self.read_upgrade is not None:
            value = self.read_upgrade(value)
        return self.adapter.validate_python(value)

    def compare_values(self, x, y) -> bool:
        return x == y


def make_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def migration_engine(database_url: str) -> Engine:
    """An engine for schema migrations, separate from the app's.

    Python's sqlite3 module only opens a transaction before INSERT, UPDATE or DELETE, so DDL
    would commit statement by statement; here SQLAlchemy issues BEGIN itself and a failed
    migration rolls back completely. Foreign keys stay off (SQLite's default) so a table that a
    migration rebuilds can be dropped without cascading deletes into the tables that refer to it.
    """
    engine = create_engine(database_url, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _autocommit_driver(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def _alembic_config(connection=None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def init_db(engine: Engine) -> None:
    """Bring the database to the current schema by running Alembic's ``upgrade head``.

    A new database gets every table. A database from before migrations (no version table) is
    adopted by the baseline revision, which adds what the older additive upgrade would have
    added and changes nothing that is already there. Before migrating a database that already
    has tables, a copy is kept next to it. Runs in one transaction, so a failure changes nothing.
    """
    from . import models  # noqa: F401  (registers the tables on Base.metadata)

    database = engine.url.database
    if not database or database == ":memory:":
        raise ValueError("ResuApply's database must be a file")
    migrator = migration_engine(engine.url.render_as_string(hide_password=False))
    try:
        with migrator.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
            head = head_revision()
            existing = bool(inspect(connection).get_table_names())
            connection.rollback()
            if current != head and existing:
                _backup(connection, Path(database), current, head)
            with connection.begin():
                broken = _broken_references(connection)
                command.upgrade(_alembic_config(connection), "head")
                if _broken_references(connection) > broken:
                    raise RuntimeError("The database upgrade would have broken references between rows. "
                                       "It was rolled back, so nothing changed.")
    finally:
        migrator.dispose()


def _broken_references(connection) -> int:
    """Rows whose foreign key points at nothing. Counted before and after, since foreign keys are off."""
    return len(connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall())


def _backup(connection, database: Path, current: str | None, head: str) -> Path:
    """Copy the database before an upgrade, as ``resuapply.db.before-<revision>.bak``.

    An existing copy for the same upgrade is kept: it is the older one, made before any attempt.
    """
    target = database.with_name(f"{database.name}.before-{head}.bak")
    if not target.exists():
        source = connection.connection.driver_connection
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
    logger.info("database upgrade from=%s to=%s backup=%s", current or "unversioned", head, target.name)
    return target


def get_ai_client(request: Request):
    """FastAPI dependency: the shared OpenRouter client."""
    return request.app.state.ai_client


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
