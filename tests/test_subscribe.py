"""Tests for the double opt-in email subscription flow."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from signalweek.db import (
    SUBSCRIBER_STATUS_ACTIVE,
    SUBSCRIBER_STATUS_PENDING,
    Subscriber,
    create_session_factory,
)
from signalweek.web import build_app


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(session_factory=session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _load_subscriber(
    session_factory: async_sessionmaker[AsyncSession], email: str
) -> Subscriber | None:
    async with session_factory() as session:
        return (
            await session.execute(select(Subscriber).where(Subscriber.email == email))
        ).scalar_one_or_none()


class TestSubscribe:
    async def test_happy_path_creates_pending_subscriber(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        response = await client.post("/subscribe", json={"email": "reader@example.com"})

        assert response.status_code == 201
        payload = response.json()
        assert payload == {"status": SUBSCRIBER_STATUS_PENDING, "email": "reader@example.com"}
        # Token is not exposed in the response.
        assert "token" not in payload
        assert "confirmation_token" not in payload

        subscriber = await _load_subscriber(session_factory, "reader@example.com")
        assert subscriber is not None
        assert subscriber.status == SUBSCRIBER_STATUS_PENDING
        assert subscriber.confirmed_at is None
        assert subscriber.confirmation_token
        # Token should be long enough to be unguessable.
        assert len(subscriber.confirmation_token) >= 32

    async def test_normalizes_email_case_and_whitespace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        response = await client.post(
            "/subscribe", json={"email": "  Reader@Example.COM  "}
        )
        assert response.status_code == 201

        subscriber = await _load_subscriber(session_factory, "reader@example.com")
        assert subscriber is not None

    async def test_accepts_urlencoded_form_body(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        response = await client.post(
            "/subscribe", data={"email": "form@example.com"}
        )
        assert response.status_code == 201
        subscriber = await _load_subscriber(session_factory, "form@example.com")
        assert subscriber is not None
        assert subscriber.status == SUBSCRIBER_STATUS_PENDING

    async def test_missing_email_returns_400(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/subscribe", json={})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid email"

    async def test_invalid_email_returns_400(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/subscribe", json={"email": "not-an-email"})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid email"

    async def test_duplicate_email_returns_409(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        first = await client.post("/subscribe", json={"email": "dup@example.com"})
        assert first.status_code == 201

        first_subscriber = await _load_subscriber(session_factory, "dup@example.com")
        assert first_subscriber is not None
        original_token = first_subscriber.confirmation_token

        second = await client.post("/subscribe", json={"email": "dup@example.com"})
        assert second.status_code == 409
        assert second.json()["error"] == "already subscribed"

        # No second row was created and the token was left untouched.
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(Subscriber).where(Subscriber.email == "dup@example.com")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].confirmation_token == original_token

    async def test_duplicate_check_is_case_insensitive(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        first = await client.post("/subscribe", json={"email": "case@example.com"})
        assert first.status_code == 201

        second = await client.post("/subscribe", json={"email": "CASE@Example.com"})
        assert second.status_code == 409

        async with session_factory() as session:
            count = len(
                list(
                    (await session.execute(select(Subscriber))).scalars().all()
                )
            )
        assert count == 1


class TestConfirm:
    async def test_valid_token_activates_subscriber(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        create = await client.post(
            "/subscribe", json={"email": "confirm@example.com"}
        )
        assert create.status_code == 201

        subscriber = await _load_subscriber(session_factory, "confirm@example.com")
        assert subscriber is not None
        token = subscriber.confirmation_token

        response = await client.get("/subscribe/confirm", params={"token": token})
        assert response.status_code == 200
        payload = response.json()
        assert payload == {
            "status": SUBSCRIBER_STATUS_ACTIVE,
            "email": "confirm@example.com",
        }

        refreshed = await _load_subscriber(session_factory, "confirm@example.com")
        assert refreshed is not None
        assert refreshed.status == SUBSCRIBER_STATUS_ACTIVE
        assert refreshed.confirmed_at is not None

    async def test_confirm_is_idempotent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        await client.post("/subscribe", json={"email": "again@example.com"})
        subscriber = await _load_subscriber(session_factory, "again@example.com")
        assert subscriber is not None
        token = subscriber.confirmation_token

        first = await client.get("/subscribe/confirm", params={"token": token})
        second = await client.get("/subscribe/confirm", params={"token": token})

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == SUBSCRIBER_STATUS_ACTIVE

    async def test_bad_token_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(
            "/subscribe/confirm", params={"token": "does-not-exist"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "invalid token"

    async def test_missing_token_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/subscribe/confirm")
        assert response.status_code == 404
        assert response.json()["error"] == "invalid token"

    async def test_bad_token_does_not_touch_other_subscribers(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        await client.post("/subscribe", json={"email": "intact@example.com"})

        response = await client.get(
            "/subscribe/confirm", params={"token": "wrong-token"}
        )
        assert response.status_code == 404

        subscriber = await _load_subscriber(session_factory, "intact@example.com")
        assert subscriber is not None
        assert subscriber.status == SUBSCRIBER_STATUS_PENDING
        assert subscriber.confirmed_at is None
