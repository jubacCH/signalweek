"""Tests for :mod:`signalweek.config`."""

from __future__ import annotations

import pytest

from signalweek.config import Settings, get_settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.app_name == "signalweek"
    assert settings.environment == "development"
    assert settings.debug is False


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWEEK_ENVIRONMENT", "production")
    monkeypatch.setenv("SIGNALWEEK_DEBUG", "true")

    settings = Settings()

    assert settings.environment == "production"
    assert settings.debug is True


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        first = get_settings()
        second = get_settings()
        assert first is second
    finally:
        get_settings.cache_clear()
