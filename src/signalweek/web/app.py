"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from signalweek.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    A caller may pass a pre-built :class:`Settings` instance (useful in tests);
    otherwise the process-wide cached settings are used.
    """

    resolved = settings if settings is not None else get_settings()
    app = FastAPI(title=resolved.app_name, debug=resolved.debug)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
