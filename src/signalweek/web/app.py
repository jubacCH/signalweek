"""FastAPI application factory.

The web layer is intentionally tiny for the curated-digest pivot: a public
landing page and a health check. Every per-user surface (sign-up, log in,
sources CRUD, personal digest) has been retired — the product is a fixed
weekly publication with no user accounts.

Note: this module intentionally does not use ``from __future__ import
annotations`` — FastAPI resolves route signatures with ``get_type_hints``.
"""

from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


def create_app() -> FastAPI:
    """Build a configured FastAPI application."""

    app = FastAPI(title="Signalweek", docs_url="/docs", redoc_url=None)

    package_root = files("signalweek.web")
    templates_dir = str(package_root / "templates")
    static_dir = str(package_root / "static")
    templates = Jinja2Templates(directory=templates_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "landing.html.j2",
            {"title": "Signalweek"},
        )

    return app
