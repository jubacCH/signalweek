"""JSON HTTP API router.

Exposes three read/write endpoints under ``/api``:

* ``/api/sources`` — list, create, delete a caller's feed sources.
* ``/api/signals`` — list a caller's in-database signals (with optional filter
  by ``source_id`` and offset/limit pagination).
* ``/api/digest/{iso_week}`` — build and return the ranked digest for the
  given ISO week (``YYYY-Www``) from the caller's live signals.

All endpoints require a bearer token in the ``Authorization`` header. The
plaintext token is looked up by its SHA-256 hash against :class:`ApiToken`.
FastAPI automatically wires the endpoints into the OpenAPI schema served at
``/docs``.

Note: this module intentionally does not use ``from __future__ import
annotations`` — FastAPI resolves route signatures via ``get_type_hints`` and
some of the annotations below (``SessionDep``, ``UserDep``) are defined inside
``build_api_router`` and therefore aren't visible in module globals.
"""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signalweek.db.models import Signal, Source, User
from signalweek.db.repositories import (
    ApiTokenRepository,
    SourceRepository,
    UserRepository,
)
from signalweek.digest import build_digest
from signalweek.web.tokens import hash_token
from signalweek.web.validate import FeedValidationError, ValidatedFeed

FeedValidator = Callable[[str], ValidatedFeed]

MAX_SIGNALS_LIMIT = 500
DEFAULT_SIGNALS_LIMIT = 50


class SourceOut(BaseModel):
    """A user's feed source as returned by the JSON API."""

    id: int
    url: str
    title: str | None
    type: str
    created_at: datetime


class SourceCreate(BaseModel):
    """Request body for ``POST /api/sources``."""

    url: str = Field(..., description="Feed URL to subscribe to.")


class SignalOut(BaseModel):
    """A single stored signal as returned by the JSON API."""

    id: int
    source_id: int
    guid: str
    title: str
    url: str
    summary: str | None
    published_at: datetime | None
    created_at: datetime


class DigestItemOut(BaseModel):
    title: str
    url: str
    summary: str | None
    published_at: datetime | None
    score: float


class DigestSectionOut(BaseModel):
    source_title: str
    source_url: str
    items: list[DigestItemOut]


class DigestOut(BaseModel):
    """A rendered weekly digest for one user, returned as JSON."""

    iso_week: str
    week_start: date
    week_end: date
    user_email: str
    sections: list[DigestSectionOut]


class ErrorOut(BaseModel):
    detail: str


def _parse_bearer(header_value: str | None) -> str:
    if not header_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


_ISO_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")


def _parse_iso_week(iso_week: str) -> tuple[date, datetime, datetime]:
    """Parse ``YYYY-Www`` into Monday, and its UTC week window bounds."""
    match = _ISO_WEEK_RE.fullmatch(iso_week)
    if match is None:
        raise HTTPException(status_code=404, detail="Invalid ISO week.")
    year, week = int(match.group(1)), int(match.group(2))
    try:
        week_start = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid ISO week.") from exc
    window_start = datetime.combine(week_start, time.min, tzinfo=UTC)
    return week_start, window_start, window_start + timedelta(days=7)


def build_api_router(
    session_dependency: Callable[..., Iterator[Session]],
    feed_validator: FeedValidator,
) -> APIRouter:
    """Return an ``APIRouter`` mounted under ``/api`` with token auth wired in."""

    router = APIRouter(prefix="/api", tags=["api"])

    def _get_session() -> Iterator[Session]:
        yield from session_dependency()

    SessionDep = Annotated[Session, Depends(_get_session)]

    def _require_api_user(
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> User:
        token = _parse_bearer(authorization)
        record = ApiTokenRepository(session).get_by_hash(hash_token(token))
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = UserRepository(session).get(record.user_id)
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token owner is inactive.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    UserDep = Annotated[User, Depends(_require_api_user)]

    unauthorized: dict[int | str, dict[str, object]] = {
        401: {"model": ErrorOut, "description": "Missing or invalid bearer token."},
    }

    @router.get(
        "/sources",
        response_model=list[SourceOut],
        responses=unauthorized,
        summary="List the caller's feed sources.",
    )
    def list_sources(session: SessionDep, current_user: UserDep) -> list[SourceOut]:
        rows = SourceRepository(session).list_for_user(current_user.id)
        return [_source_to_out(s) for s in rows]

    @router.post(
        "/sources",
        response_model=SourceOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            **unauthorized,
            409: {"model": ErrorOut, "description": "Source already exists for the user."},
            422: {"model": ErrorOut, "description": "URL failed feed validation."},
        },
        summary="Add a new feed source for the caller.",
    )
    def create_source(
        payload: SourceCreate,
        session: SessionDep,
        current_user: UserDep,
    ) -> SourceOut:
        raw_url = (payload.url or "").strip()
        try:
            validated = feed_validator(raw_url)
        except FeedValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        repo = SourceRepository(session)
        if any(s.url == validated.url for s in repo.list_for_user(current_user.id)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That source is already in your list.",
            )
        try:
            source = repo.create(
                user_id=current_user.id,
                url=validated.url,
                title=validated.title,
                type=validated.feed_type,
            )
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That source is already in your list.",
            ) from exc
        return _source_to_out(source)

    @router.delete(
        "/sources/{source_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            **unauthorized,
            404: {"model": ErrorOut, "description": "Source not found for the caller."},
        },
        summary="Remove one of the caller's feed sources.",
    )
    def delete_source(
        source_id: int,
        session: SessionDep,
        current_user: UserDep,
    ) -> Response:
        removed = SourceRepository(session).delete_for_user(source_id, current_user.id)
        if not removed:
            raise HTTPException(status_code=404, detail="Source not found.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/signals",
        response_model=list[SignalOut],
        responses=unauthorized,
        summary="List stored signals across the caller's sources.",
    )
    def list_signals(
        session: SessionDep,
        current_user: UserDep,
        source_id: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_SIGNALS_LIMIT)] = DEFAULT_SIGNALS_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[SignalOut]:
        owned_ids = [s.id for s in SourceRepository(session).list_for_user(current_user.id)]
        if source_id is not None:
            if source_id not in owned_ids:
                # A source that isn't owned by the caller is indistinguishable
                # from one that doesn't exist — say the list is empty rather
                # than leaking existence.
                return []
            filter_ids = [source_id]
        else:
            filter_ids = owned_ids
        if not filter_ids:
            return []
        stmt = (
            select(Signal)
            .where(Signal.source_id.in_(filter_ids))
            .order_by(Signal.published_at.desc().nulls_last(), Signal.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [_signal_to_out(s) for s in rows]

    @router.get(
        "/digest/{iso_week}",
        response_model=DigestOut,
        responses={
            **unauthorized,
            404: {"model": ErrorOut, "description": "Malformed ISO week."},
        },
        summary="Build the caller's digest for the given ISO week.",
    )
    def get_digest(
        iso_week: str,
        session: SessionDep,
        current_user: UserDep,
    ) -> DigestOut:
        week_start, window_start, window_end = _parse_iso_week(iso_week)
        digest = build_digest(
            session,
            current_user,
            window_start=window_start,
            window_end=window_end,
            now=window_end,
        )
        return DigestOut(
            iso_week=iso_week,
            week_start=week_start,
            week_end=week_start + timedelta(days=7),
            user_email=digest.user_email,
            sections=[
                DigestSectionOut(
                    source_title=section.source_title,
                    source_url=section.source_url,
                    items=[
                        DigestItemOut(
                            title=item.title,
                            url=item.url,
                            summary=item.summary,
                            published_at=item.published_at,
                            score=item.score,
                        )
                        for item in section.items
                    ],
                )
                for section in digest.sections
            ],
        )

    return router


def _source_to_out(source: Source) -> SourceOut:
    return SourceOut(
        id=source.id,
        url=source.url,
        title=source.title,
        type=source.type,
        created_at=source.created_at,
    )


def _signal_to_out(signal: Signal) -> SignalOut:
    return SignalOut(
        id=signal.id,
        source_id=signal.source_id,
        guid=signal.guid,
        title=signal.title,
        url=signal.url,
        summary=signal.summary,
        published_at=signal.published_at,
        created_at=signal.created_at,
    )
