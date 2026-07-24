"""Application settings sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the signalweek service.

    Values are loaded from environment variables prefixed with ``SIGNALWEEK_``
    and, when present, from a ``.env`` file in the working directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="SIGNALWEEK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "signalweek"
    environment: str = "development"
    debug: bool = False
    database_url: str = "sqlite+aiosqlite:///./signalweek.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""

    return Settings()
