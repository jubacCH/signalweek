"""ASGI entry point used by the Dockerfile and ``uvicorn signalweek.main:app``."""

from __future__ import annotations

from signalweek.web import create_app

app = create_app()
