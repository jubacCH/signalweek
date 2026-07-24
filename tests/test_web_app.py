"""Tests for the FastAPI application skeleton."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from signalweek.config import Settings
from signalweek.web.app import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(app_name="signalweek-test", environment="test", debug=True)


async def test_healthz_returns_ok(settings: Settings) -> None:
    app = create_app(settings)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_app_uses_settings_metadata(settings: Settings) -> None:
    app = create_app(settings)

    assert app.title == "signalweek-test"
    assert app.debug is True


async def test_create_app_without_arguments_uses_defaults() -> None:
    app = create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
