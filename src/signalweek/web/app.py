"""FastAPI application factory.

The web layer is intentionally tiny for the curated-digest pivot: a public
landing page and a health check. Every per-user surface (sign-up, log in,
sources CRUD, personal digest) has been retired — the product is a fixed
weekly publication with no user accounts.

Note: this module intentionally does not use ``from __future__ import
annotations`` — FastAPI resolves route signatures with ``get_type_hints``.
"""

import contextlib
import logging
from datetime import date
from importlib.resources import files

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.engine import Engine

from signalweek.db.session import get_engine
from signalweek.ingest.classify import CATEGORIES, CATEGORY_LABELS
from signalweek.web.admin import register_admin_router, resolve_admin_token
from signalweek.web.archive import (
    load_published_issue_by_week,
    load_published_issues,
)
from signalweek.web.landing import load_latest_published_issue
from signalweek.web.renderers import render_issue
from signalweek.db.session import create_session_factory
from signalweek.scheduler import create_scheduler
from signalweek.sources import seed_sources_if_empty

_logger = logging.getLogger(__name__)

PRODUCT_NAME = "Signalweek"
PRODUCT_TAGLINE = (
    "A curated weekly digest of the AI industry — five fixed categories, "
    "every item citing a primary source."
)


def create_app(
    engine: Engine | None = None,
    admin_token: str | None = None,
    *,
    start_background: bool | None = None,
    scheduler=None,
) -> FastAPI:
    """Build a configured FastAPI application.

    ``engine`` lets tests inject an isolated database; production callers
    can omit it and the app resolves the process-wide engine lazily on
    every request. ``admin_token`` overrides the ``SIGNALWEEK_ADMIN_TOKEN``
    environment variable — when neither is set the ``/admin`` routes are
    mounted but respond with 503 so the surface is discoverable without
    being exploitable.
    """

    _start_background = (engine is None) if start_background is None else start_background

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        sched = None
        if _start_background:
            eng = _resolve_engine()
            try:
                with eng.begin() as conn:
                    seeded = seed_sources_if_empty(conn)
                if seeded:
                    _logger.info("seeded %d sources on startup", seeded)
            except Exception:
                _logger.exception("startup source seeding failed")
            try:
                sched = create_scheduler(create_session_factory(eng), scheduler=scheduler)
                sched.start()
                app.state.scheduler = sched
                _logger.info("scheduler started: jobs=%s", [j.id for j in sched.get_jobs()])
            except Exception:
                _logger.exception("scheduler start failed")
        yield
        if sched is not None:
            with contextlib.suppress(Exception):
                sched.shutdown(wait=False)

    app = FastAPI(title="Signalweek", docs_url="/docs", redoc_url=None, lifespan=_lifespan)

    package_root = files("signalweek.web")
    templates_dir = str(package_root / "templates")
    static_dir = str(package_root / "static")
    # Templates use the ``.html.j2`` extension; a fresh Jinja Environment lets
    # us pass ``select_autoescape`` explicitly so ``.j2`` files still get HTML
    # escaping (Starlette's default only escapes ``.html``/``.xml``).
    jinja_env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml", "html.j2", "j2"),
            default_for_string=True,
        ),
    )
    templates = Jinja2Templates(env=jinja_env)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def _resolve_engine() -> Engine:
        return engine if engine is not None else get_engine()

    register_admin_router(
        app,
        _resolve_engine,
        admin_token=resolve_admin_token(admin_token),
    )

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        latest = load_latest_published_issue(_resolve_engine())
        return templates.TemplateResponse(
            request,
            "landing.html.j2",
            {
                "title": PRODUCT_NAME,
                "product_name": PRODUCT_NAME,
                "product_tagline": PRODUCT_TAGLINE,
                "latest_issue": latest,
                "categories": CATEGORIES,
                "category_labels": CATEGORY_LABELS,
                "nav_current": "home",
            },
        )

    @app.get("/issues", response_class=HTMLResponse)
    def issues_index(request: Request) -> HTMLResponse:
        issues = load_published_issues(_resolve_engine())
        return templates.TemplateResponse(
            request,
            "issues_index.html.j2",
            {
                "title": "Archive",
                "issues": issues,
                "nav_current": "archive",
            },
        )

    @app.get("/issues/{week_of}", response_class=HTMLResponse)
    def issue_permalink(week_of: str) -> HTMLResponse:
        try:
            parsed_week = date.fromisoformat(week_of)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="issue not found") from exc

        detail = load_published_issue_by_week(_resolve_engine(), parsed_week)
        if detail is None:
            raise HTTPException(status_code=404, detail="issue not found")

        html = render_issue(
            week_of=detail.week_of,
            status="published",
            published_at=detail.published_at,
            items=detail.items,
        )
        return HTMLResponse(content=html)

    return app
