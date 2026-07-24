"""Shared pytest fixtures for the signalweek test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from signalweek.db import Base, create_engine, create_session_factory


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Provide an in-memory SQLite async engine with the schema loaded."""

    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Provide an :class:`AsyncSession` bound to the in-memory engine."""

    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
