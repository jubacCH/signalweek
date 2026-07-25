"""Public web app: reads persisted :class:`Issue` rows and renders HTML."""

from __future__ import annotations

from signalweek.web.app import build_app

__all__ = ["build_app"]
