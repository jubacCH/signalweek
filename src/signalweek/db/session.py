"""Async engine and session factory helpers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from signalweek.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for the given database URL."""

    return create_async_engine(url, echo=echo, future=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an :class:`async_sessionmaker` bound to ``engine``."""

    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""

    global _engine
    if _engine is None:
        resolved = settings if settings is not None else get_settings()
        _engine = create_engine(resolved.database_url)
    return _engine


def get_session_factory(
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first use."""

    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine(settings))
    return _session_factory


async def reset_engine() -> None:
    """Dispose of the cached engine and factory (for tests / shutdown)."""

    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
