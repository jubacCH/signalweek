"""Data layer: engine and session factory for the curated-digest schema.

The curated-digest pipeline defines its tables as SQLAlchemy Core
:class:`Table` objects on :data:`signalweek.sources.sources_metadata`; no
declarative ORM base lives here. Migrations under ``alembic/versions`` are
the sole source of truth for the shipped schema.
"""

from signalweek.db.session import (
    create_db_engine,
    create_session_factory,
    get_database_url,
    get_engine,
    get_session_factory,
    reset_engine,
    session_scope,
)

__all__ = [
    "create_db_engine",
    "create_session_factory",
    "get_database_url",
    "get_engine",
    "get_session_factory",
    "reset_engine",
    "session_scope",
]
