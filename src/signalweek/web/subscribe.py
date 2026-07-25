"""Request handlers for the double opt-in email subscription flow.

``POST /subscribe`` stores a pending :class:`Subscriber` with a random
confirmation token. ``GET /subscribe/confirm?token=...`` swaps the
subscriber to ``active`` once the token is presented.

Sending the confirmation email is out of scope for this task — the
token lives in the database and is delivered by future work.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from signalweek.db.models import (
    SUBSCRIBER_STATUS_ACTIVE,
    SUBSCRIBER_STATUS_PENDING,
    Subscriber,
)

RouteHandler = Callable[[Request], Awaitable[Response]]

# Deliberately permissive — validates shape, not deliverability.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


async def _read_email(request: Request) -> str | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    raw: object
    if content_type == "application/json":
        try:
            payload = await request.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        raw = payload.get("email")
    elif content_type == "application/x-www-form-urlencoded":
        body = await request.body()
        parsed = parse_qs(body.decode("utf-8", errors="replace"))
        values = parsed.get("email")
        raw = values[0] if values else None
    else:
        return None
    if not isinstance(raw, str):
        return None
    return _normalize_email(raw)


@dataclass(frozen=True)
class SubscribeHandlers:
    """Bundle of subscription route handlers."""

    subscribe: RouteHandler
    confirm: RouteHandler


def build_subscribe_routes(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    token_factory: Callable[[], str] = _generate_token,
) -> SubscribeHandlers:
    """Bind the session factory to the subscription route callables."""

    async def subscribe(request: Request) -> Response:
        email = await _read_email(request)
        if email is None or not _is_valid_email(email):
            return JSONResponse({"error": "invalid email"}, status_code=400)

        async with session_factory() as session:
            existing = (
                await session.execute(
                    select(Subscriber).where(Subscriber.email == email)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return JSONResponse(
                    {"error": "already subscribed", "status": existing.status},
                    status_code=409,
                )

            subscriber = Subscriber(
                email=email,
                confirmation_token=token_factory(),
                status=SUBSCRIBER_STATUS_PENDING,
            )
            session.add(subscriber)
            await session.commit()

        return JSONResponse(
            {"status": SUBSCRIBER_STATUS_PENDING, "email": email},
            status_code=201,
        )

    async def confirm(request: Request) -> Response:
        token = request.query_params.get("token", "").strip()
        if not token:
            return JSONResponse({"error": "invalid token"}, status_code=404)

        async with session_factory() as session:
            subscriber = (
                await session.execute(
                    select(Subscriber).where(
                        Subscriber.confirmation_token == token
                    )
                )
            ).scalar_one_or_none()
            if subscriber is None:
                return JSONResponse({"error": "invalid token"}, status_code=404)

            if subscriber.status != SUBSCRIBER_STATUS_ACTIVE:
                subscriber.status = SUBSCRIBER_STATUS_ACTIVE
                subscriber.confirmed_at = datetime.now(UTC)
                await session.commit()

            email = subscriber.email

        return JSONResponse(
            {"status": SUBSCRIBER_STATUS_ACTIVE, "email": email},
            status_code=200,
        )

    return SubscribeHandlers(subscribe=subscribe, confirm=confirm)
