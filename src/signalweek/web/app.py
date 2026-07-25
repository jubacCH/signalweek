"""Starlette application factory for the public SignalWeek website.

The app is intentionally minimal:

* three routes: latest issue, archive index, and a single issue view;
* Jinja2 templates rendered from ``signalweek/web/templates``;
* a single stylesheet served from ``signalweek/web/static``.

The database session factory is injected so tests can point the app at
an in-memory engine.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from signalweek.db.session import create_session_factory
from signalweek.web.routes import build_routes
from signalweek.web.subscribe import build_subscribe_routes

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def build_templates() -> Environment:
    """Return the Jinja2 environment used by the web app."""

    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_app(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    engine: AsyncEngine | None = None,
) -> Starlette:
    """Assemble the public Starlette app.

    Either a ``session_factory`` or an ``engine`` must be supplied; when
    only an engine is given a session factory is derived from it.
    """

    if session_factory is None:
        if engine is None:
            raise ValueError("build_app requires session_factory or engine")
        session_factory = create_session_factory(engine)

    templates = build_templates()
    handlers = build_routes(session_factory=session_factory, templates=templates)
    subscribe_handlers = build_subscribe_routes(session_factory=session_factory)

    routes: list[Route | Mount] = [
        Route("/", handlers.index, name="index"),
        Route("/issues", handlers.archive, name="archive"),
        Route("/issues/{iso_week}.md", handlers.issue_markdown, name="issue_markdown"),
        Route("/issues/{iso_week}.json", handlers.issue_json, name="issue_json"),
        Route("/issues/{iso_week}", handlers.issue, name="issue"),
        Route(
            "/subscribe",
            subscribe_handlers.subscribe,
            methods=["POST"],
            name="subscribe",
        ),
        Route(
            "/subscribe/confirm",
            subscribe_handlers.confirm,
            methods=["GET"],
            name="subscribe_confirm",
        ),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    return Starlette(routes=routes)
