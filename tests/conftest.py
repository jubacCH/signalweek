"""Shared pytest fixtures for the Signalweek test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from signalweek.db.session import create_db_engine
from signalweek.sources import sources_metadata


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    """A fresh in-memory SQLite engine with the curated-digest schema created.

    Mirrors what ``alembic upgrade head`` produces: the five tables in
    :data:`signalweek.sources.sources_metadata` and their indexes.
    """
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
