"""Async engine and session factory helpers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for the given database URL."""

    return create_async_engine(url, echo=echo, future=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an :class:`async_sessionmaker` bound to ``engine``."""

    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
