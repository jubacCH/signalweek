"""FastAPI web layer: public landing page, health check, and issue renderer."""

from signalweek.web.app import create_app
from signalweek.web.renderers import render_issue

__all__ = ["create_app", "render_issue"]
