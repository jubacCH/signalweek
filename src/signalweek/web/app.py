"""FastAPI application factory.

The web layer stays intentionally small: a landing page, a health check, and a
sign-up form that creates a local :class:`~signalweek.db.models.User`. The DB
session dependency defers to :mod:`signalweek.db.session` by default so tests
can override it with an in-memory SQLite engine.

Note: this module intentionally does not use ``from __future__ import
annotations`` — FastAPI resolves route signatures with ``get_type_hints``, and
the session dependency is defined inside ``create_app`` so it isn't visible in
module globals.
"""

from collections.abc import Callable, Iterator
from importlib.resources import files
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signalweek.db.repositories import UserRepository
from signalweek.db.session import get_session_factory
from signalweek.web.security import hash_password

MIN_PASSPHRASE_LENGTH = 12


def _default_session_dependency() -> Iterator[Session]:
    """Yield a database session from the process-wide session factory."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _normalize_email(raw: str) -> str:
    return raw.strip().lower()


def _validate_signup(email: str, passphrase: str) -> str | None:
    """Return an error message for invalid input, or ``None`` if it's fine."""
    if not email or "@" not in email or "." not in email.split("@", 1)[1]:
        return "Please enter a valid email address."
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        return f"Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters long."
    return None


def create_app(
    session_dependency: Callable[..., Iterator[Session]] | None = None,
) -> FastAPI:
    """Build a configured FastAPI application.

    Passing ``session_dependency`` lets tests inject an in-memory DB session
    without touching the process-wide session factory.
    """

    app = FastAPI(title="Signalweek", docs_url=None, redoc_url=None)

    package_root = files("signalweek.web")
    templates_dir = str(package_root / "templates")
    static_dir = str(package_root / "static")
    templates = Jinja2Templates(directory=templates_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    session_provider = session_dependency or _default_session_dependency

    def _get_session() -> Iterator[Session]:
        yield from session_provider()

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

    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "signup.html.j2",
            {
                "title": "Sign up",
                "min_passphrase_length": MIN_PASSPHRASE_LENGTH,
            },
        )

    @app.post("/signup", response_class=HTMLResponse, response_model=None)
    def signup_submit(
        request: Request,
        email: Annotated[str, Form()],
        passphrase: Annotated[str, Form()],
        session: Annotated[Session, Depends(_get_session)],
    ) -> Response:
        normalized_email = _normalize_email(email)
        error = _validate_signup(normalized_email, passphrase)
        if error is None:
            users = UserRepository(session)
            if users.get_by_email(normalized_email) is not None:
                error = "An account already exists for that email."
            else:
                try:
                    users.create(
                        email=normalized_email,
                        hashed_password=hash_password(passphrase),
                    )
                except IntegrityError:
                    session.rollback()
                    error = "An account already exists for that email."
                else:
                    return RedirectResponse(url="/welcome", status_code=status.HTTP_303_SEE_OTHER)

        return templates.TemplateResponse(
            request,
            "signup.html.j2",
            {
                "title": "Sign up",
                "min_passphrase_length": MIN_PASSPHRASE_LENGTH,
                "error": error,
                "email": normalized_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "welcome.html.j2",
            {"title": "Welcome"},
        )

    return app
