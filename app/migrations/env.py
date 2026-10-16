"""Alembic environment for ResuApply's SQLite database.

At startup ``app.db.init_db`` runs ``upgrade head`` and passes its own connection, which already
holds the migration transaction. Run from the command line (``alembic upgrade head``,
``alembic revision --autogenerate -m "..."``) it opens the database named by the app's settings.
"""

from __future__ import annotations

from alembic import context

from app import models  # noqa: F401  (registers the tables on Base.metadata)
from app.db import Base, PydanticJSON, UTCDateTime, migration_engine

config = context.config


def render_item(type_, obj, autogen_context):
    """Write the app's column classes as the plain types they store, so migrations stay frozen."""
    if type_ == "type" and isinstance(obj, PydanticJSON):
        return "sa.JSON()"
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def run(connection) -> None:
    # Batch mode rebuilds a table for changes SQLite's ALTER TABLE can't make.
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True,
                      render_item=render_item, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("ResuApply migrations run against the database; offline (--sql) mode is not supported.")

connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    from app.config import get_settings

    engine = migration_engine(get_settings().database_url)
    try:
        with engine.connect() as cli_connection:
            run(cli_connection)
            cli_connection.commit()
    finally:
        engine.dispose()
