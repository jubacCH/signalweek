"""Database engine and session factory.

The engine URL is read from ``DATABASE_URL`` and defaults to a local SQLite
file. The engine and session factory are constructed lazily on first access so
that importing this module does not require a reachable database or optional
dialect drivers.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///./signalweek.db"


def get_database_url() -> str:
    """Return the configured database URL, or the local SQLite default."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(url: str | None = None, **kwargs: object) -> Engine:
    """Create a SQLAlchemy engine with sensible defaults for SQLite."""
    resolved_url = url or get_database_url()
    connect_args: dict[str, object] = {}
    if resolved_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(resolved_url, connect_args=connect_args, future=True, **kwargs)


def create_session_factory(bound_engine: Engine) -> sessionmaker[Session]:
    """Build a ``sessionmaker`` bound to the given engine."""
    return sessionmaker(bind=bound_engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: object, _record: object) -> None:
    """Turn ON PRAGMA foreign_keys for every SQLite connection."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first access."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, creating it on first access."""
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


def reset_engine() -> None:
    """Drop the cached engine and factory. Used by tests or config reloads."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
