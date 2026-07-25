"""FastAPI application factory.

The web layer stays intentionally small: a landing page, a health check,
sign-up / login, and a sources CRUD page powered by HTMX. The DB session
dependency defers to :mod:`signalweek.db.session` by default so tests can
override it with an in-memory SQLite engine. The feed validator is likewise
injectable so tests can drive the ``httpx`` client with a ``MockTransport``
and skip the real network.

Note: this module intentionally does not use ``from __future__ import
annotations`` — FastAPI resolves route signatures with ``get_type_hints``, and
the session dependency is defined inside ``create_app`` so it isn't visible in
module globals.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signalweek.db.models import User
from signalweek.db.repositories import SourceRepository, UserRepository
from signalweek.db.session import get_session_factory
from signalweek.digest import build_digest
from signalweek.web.security import hash_password, verify_password
from signalweek.web.sessions import (
    clear_session_cookie,
    get_current_user_id,
    set_session_cookie,
)
from signalweek.web.validate import FeedValidationError, ValidatedFeed, validate_feed_url

MIN_PASSPHRASE_LENGTH = 12

FeedValidator = Callable[[str], ValidatedFeed]
Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


def current_week_window(now: datetime) -> tuple[datetime, datetime]:
    """Return ``[Monday 00:00 UTC, next Monday 00:00 UTC)`` bracketing ``now``."""
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    aware = aware.astimezone(UTC)
    start_of_day = aware.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = start_of_day - timedelta(days=aware.weekday())
    return monday, monday + timedelta(days=7)


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
    feed_validator: FeedValidator | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build a configured FastAPI application.

    Passing ``session_dependency`` lets tests inject an in-memory DB session
    without touching the process-wide session factory. Passing
    ``feed_validator`` lets tests replace the real ``httpx``-backed probe with
    a stub that returns canned :class:`ValidatedFeed` values or raises
    :class:`FeedValidationError`. Passing ``clock`` lets tests freeze "now" so
    the in-progress week window is deterministic.
    """

    app = FastAPI(title="Signalweek", docs_url=None, redoc_url=None)

    package_root = files("signalweek.web")
    templates_dir = str(package_root / "templates")
    static_dir = str(package_root / "static")
    templates = Jinja2Templates(directory=templates_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    session_provider = session_dependency or _default_session_dependency
    validate_feed: FeedValidator = feed_validator or validate_feed_url
    now_provider: Clock = clock or _default_clock

    def _get_session() -> Iterator[Session]:
        yield from session_provider()

    def _require_current_user(
        request: Request,
        session: Annotated[Session, Depends(_get_session)],
    ) -> User:
        user_id = get_current_user_id(request)
        if user_id is not None:
            user = UserRepository(session).get(user_id)
            if user is not None and user.is_active:
                return user
        # Unauthenticated: bounce to /login. For HTMX requests we use
        # HX-Redirect so the client performs a full-page redirect.
        if request.headers.get("HX-Request"):
            raise HTTPException(status_code=401, headers={"HX-Redirect": "/login"})
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def landing(
        request: Request,
        session: Annotated[Session, Depends(_get_session)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "landing.html.j2",
            {"title": "Signalweek", "current_user": _optional_user(request, session)},
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
                    user = users.create(
                        email=normalized_email,
                        hashed_password=hash_password(passphrase),
                    )
                except IntegrityError:
                    session.rollback()
                    error = "An account already exists for that email."
                else:
                    response = RedirectResponse(
                        url="/welcome", status_code=status.HTTP_303_SEE_OTHER
                    )
                    set_session_cookie(response, user.id)
                    return response

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
    def welcome(
        request: Request,
        session: Annotated[Session, Depends(_get_session)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "welcome.html.j2",
            {"title": "Welcome", "current_user": _optional_user(request, session)},
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html.j2", {"title": "Log in"})

    @app.post("/login", response_class=HTMLResponse, response_model=None)
    def login_submit(
        request: Request,
        email: Annotated[str, Form()],
        passphrase: Annotated[str, Form()],
        session: Annotated[Session, Depends(_get_session)],
    ) -> Response:
        normalized_email = _normalize_email(email)
        user = UserRepository(session).get_by_email(normalized_email)
        if (
            user is None
            or not user.is_active
            or not verify_password(user.hashed_password, passphrase)
        ):
            return templates.TemplateResponse(
                request,
                "login.html.j2",
                {
                    "title": "Log in",
                    "error": "Email or passphrase was not recognized.",
                    "email": normalized_email,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        response = RedirectResponse(url="/sources", status_code=status.HTTP_303_SEE_OTHER)
        set_session_cookie(response, user.id)
        return response

    @app.post("/logout", response_class=HTMLResponse, response_model=None)
    def logout() -> Response:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        clear_session_cookie(response)
        return response

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(
        request: Request,
        session: Annotated[Session, Depends(_get_session)],
        current_user: Annotated[User, Depends(_require_current_user)],
    ) -> HTMLResponse:
        sources = SourceRepository(session).list_for_user(current_user.id)
        return templates.TemplateResponse(
            request,
            "sources.html.j2",
            {
                "title": "Sources",
                "sources": sources,
                "current_user": current_user,
            },
        )

    @app.post("/sources", response_class=HTMLResponse, response_model=None)
    def sources_add(
        request: Request,
        url: Annotated[str, Form()],
        session: Annotated[Session, Depends(_get_session)],
        current_user: Annotated[User, Depends(_require_current_user)],
    ) -> Response:
        raw_url = (url or "").strip()
        repo = SourceRepository(session)
        error: str | None = None
        try:
            validated = validate_feed(raw_url)
        except FeedValidationError as exc:
            error = str(exc)
        else:
            if any(s.url == validated.url for s in repo.list_for_user(current_user.id)):
                error = "That source is already in your list."
            else:
                try:
                    source = repo.create(
                        user_id=current_user.id,
                        url=validated.url,
                        title=validated.title,
                        type=validated.feed_type,
                    )
                except IntegrityError:
                    session.rollback()
                    error = "That source is already in your list."
                else:
                    return templates.TemplateResponse(
                        request,
                        "_source_added.html.j2",
                        {"source": source},
                    )

        return templates.TemplateResponse(
            request,
            "_source_form.html.j2",
            {"error": error, "url": raw_url},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @app.get("/digest", response_class=HTMLResponse)
    def digest_page(
        request: Request,
        session: Annotated[Session, Depends(_get_session)],
        current_user: Annotated[User, Depends(_require_current_user)],
    ) -> HTMLResponse:
        now = now_provider()
        window_start, window_end = current_week_window(now)
        digest = build_digest(
            session,
            current_user,
            window_start=window_start,
            window_end=window_end,
            now=now,
        )
        return templates.TemplateResponse(
            request,
            "digest.html.j2",
            {
                "title": "This week's digest",
                "current_user": current_user,
                "digest": digest,
            },
        )

    @app.delete("/sources/{source_id}", response_class=HTMLResponse, response_model=None)
    def sources_remove(
        source_id: int,
        session: Annotated[Session, Depends(_get_session)],
        current_user: Annotated[User, Depends(_require_current_user)],
    ) -> Response:
        removed = SourceRepository(session).delete_for_user(source_id, current_user.id)
        if not removed:
            raise HTTPException(status_code=404, detail="Source not found")
        # Empty body: HTMX swaps this in for the row, effectively deleting it.
        return HTMLResponse(content="", status_code=status.HTTP_200_OK)

    def _optional_user(request: Request, session: Session) -> User | None:
        user_id = get_current_user_id(request)
        if user_id is None:
            return None
        user = UserRepository(session).get(user_id)
        return user if user is not None and user.is_active else None

    return app
