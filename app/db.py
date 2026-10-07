"""Database engine, sessions and column types.

Synchronous SQLAlchemy 2.x on SQLite. Nested content is stored as JSON and validated
with Pydantic on the way in and out; identifiers, relationships, statuses and timestamps
use ordinary columns.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from pydantic import TypeAdapter
from sqlalchemy import JSON, DateTime, Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


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

    def __init__(self, pydantic_type: Any):
        super().__init__()
        self.pydantic_type = pydantic_type
        self.adapter = TypeAdapter(pydantic_type)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return self.adapter.dump_python(self.adapter.validate_python(value), mode="json")

    def process_result_value(self, value, dialect):
        return None if value is None else self.adapter.validate_python(value)

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


def init_db(engine: Engine) -> None:
    """Create any missing tables. No migration tool is used yet."""
    from . import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(engine)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
