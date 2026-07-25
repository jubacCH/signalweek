"""Shared pytest fixtures for the Signalweek test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from signalweek.db.base import Base
from signalweek.db.session import create_db_engine, create_session_factory


@pytest.fixture()
def sqlite_engine() -> Iterator[Engine]:
    """A fresh in-memory SQLite engine with the schema created."""
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def session(sqlite_engine: Engine) -> Iterator[Session]:
    """A DB session bound to the throwaway SQLite engine."""
    factory = create_session_factory(sqlite_engine)
    with factory() as s:
        yield s
