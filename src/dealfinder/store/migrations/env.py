"""Alembic environment. Used by the app's own ``upgrade`` and by the ``alembic`` command."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from dealfinder.settings import Environment
from dealfinder.store.tables import Base

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    # Batch mode lets SQLite alter tables, which it otherwise cannot do in place.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from dealfinder.store.db.upgrade
        _run(connection)
        return
    url = config.get_main_option("sqlalchemy.url") or Environment().dealfinder_db_url
    engine = create_engine(url)
    with engine.connect() as fresh:
        _run(fresh)
        fresh.commit()


run_migrations()
