"""Database engine, sessions and schema upgrades."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

MIGRATIONS = Path(__file__).parent / "migrations"


def make_engine(url: str) -> Engine:
    """Engine for ``url``. For a SQLite file the folder is created, foreign keys are switched
    on (SQLite leaves them off), and write-ahead logging lets the dashboard read while a
    sweep writes."""
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    on_disk = bool(is_sqlite and parsed.database and parsed.database != ":memory:")
    if on_disk:
        Path(str(parsed.database)).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")  # wait for another writer, in ms
            if on_disk:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def alembic_config(url: str) -> AlembicConfig:
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade(engine: Engine) -> None:
    """Bring the database to the newest schema. Safe to call on every start."""
    config = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def open_database(url: str) -> Engine:
    """Engine with the schema up to date."""
    engine = make_engine(url)
    upgrade(engine)
    return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """A session that commits on success and rolls back on any error."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
