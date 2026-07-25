"""End-to-end tests for the JSON HTTP API under ``/api``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from signalweek.db.base import Base
from signalweek.db.models import User
from signalweek.db.repositories import (
    ApiTokenRepository,
    SignalRepository,
    SourceRepository,
    UserRepository,
)
from signalweek.db.session import create_session_factory
from signalweek.web import create_app
from signalweek.web.security import hash_password
from signalweek.web.tokens import generate_token, hash_token
from signalweek.web.validate import FeedValidationError, ValidatedFeed


@pytest.fixture()
def engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


class StubValidator:
    """A tiny stand-in for :func:`validate_feed_url`."""

    def __init__(self) -> None:
        self.result: ValidatedFeed | None = None
        self.error: FeedValidationError | None = None
        self.calls: list[str] = []

    def accept(self, *, title: str | None = "Stub Feed", feed_type: str = "rss") -> None:
        self.result = ValidatedFeed(url="__placeholder__", title=title, feed_type=feed_type)
        self.error = None

    def reject(self, message: str) -> None:
        self.error = FeedValidationError(message)
        self.result = None

    def __call__(self, url: str) -> ValidatedFeed:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        assert self.result is not None, "StubValidator was not primed with a result"
        return ValidatedFeed(url=url, title=self.result.title, feed_type=self.result.feed_type)


@pytest.fixture()
def validator() -> StubValidator:
    return StubValidator()


@pytest.fixture()
def client(engine: Engine, validator: StubValidator) -> Iterator[TestClient]:
    factory = create_session_factory(engine)

    def _session_dep() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = create_app(session_dependency=_session_dep, feed_validator=validator)
    with TestClient(app) as c:
        yield c


def _make_user(engine: Engine, email: str = "ada@example.com") -> User:
    with Session(engine) as session:
        user = UserRepository(session).create(
            email=email, hashed_password=hash_password("correct horse battery staple")
        )
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def _issue_token(engine: Engine, user_id: int, name: str | None = None) -> str:
    plaintext = generate_token()
    with Session(engine) as session:
        ApiTokenRepository(session).create(
            user_id=user_id, token_hash=hash_token(plaintext), name=name
        )
        session.commit()
    return plaintext


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# OpenAPI docs
# ---------------------------------------------------------------------------


def test_openapi_docs_are_served(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    body = schema.json()
    paths = body["paths"]
    assert "/api/sources" in paths
    assert "/api/signals" in paths
    assert "/api/digest/{iso_week}" in paths
    # The Sources endpoint documents both list and create operations.
    assert set(paths["/api/sources"].keys()) >= {"get", "post"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_authorization_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/sources")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_malformed_bearer_header_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/sources", headers={"Authorization": "Token abc123"})
    assert response.status_code == 401


def test_unknown_token_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/sources", headers=_auth("sw_definitely-not-a-real-token"))
    assert response.status_code == 401


def test_inactive_user_token_is_unauthorized(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    token = _issue_token(engine, user.id)
    # Deactivate the user after the token has been minted.
    with Session(engine) as session:
        row = UserRepository(session).get(user.id)
        assert row is not None
        row.is_active = False
        session.commit()
    response = client.get("/api/sources", headers=_auth(token))
    assert response.status_code == 401


def test_hash_token_is_deterministic_and_hex() -> None:
    digest = hash_token("sw_example")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert hash_token("sw_example") == digest


def test_generate_token_is_prefixed_and_unique() -> None:
    a = generate_token()
    b = generate_token()
    assert a.startswith("sw_") and b.startswith("sw_")
    assert a != b


# ---------------------------------------------------------------------------
# /api/sources
# ---------------------------------------------------------------------------


def test_list_sources_returns_only_callers_rows(client: TestClient, engine: Engine) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    with Session(engine) as session:
        SourceRepository(session).create(
            user_id=owner.id, url="https://a.example.com/feed", title="A"
        )
        SourceRepository(session).create(
            user_id=intruder.id, url="https://b.example.com/feed", title="B"
        )
        session.commit()
    token = _issue_token(engine, owner.id)
    response = client.get("/api/sources", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert [s["url"] for s in body] == ["https://a.example.com/feed"]
    assert body[0]["title"] == "A"
    assert body[0]["type"] == "rss"
    assert "created_at" in body[0]


def test_create_source_persists_and_returns_201(
    client: TestClient, engine: Engine, validator: StubValidator
) -> None:
    user = _make_user(engine)
    token = _issue_token(engine, user.id)
    validator.accept(title="Example Blog", feed_type="atom")
    response = client.post(
        "/api/sources",
        json={"url": "https://blog.example.com/feed"},
        headers=_auth(token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == "https://blog.example.com/feed"
    assert body["title"] == "Example Blog"
    assert body["type"] == "atom"
    assert body["id"] > 0
    with Session(engine) as session:
        rows = SourceRepository(session).list_for_user(user.id)
        assert [s.url for s in rows] == ["https://blog.example.com/feed"]


def test_create_source_rejects_invalid_feed(
    client: TestClient, engine: Engine, validator: StubValidator
) -> None:
    user = _make_user(engine)
    token = _issue_token(engine, user.id)
    validator.reject("The URL was reachable but did not look like a feed.")
    response = client.post(
        "/api/sources",
        json={"url": "https://example.com/nope"},
        headers=_auth(token),
    )
    assert response.status_code == 422
    assert "did not look like a feed" in response.json()["detail"]
    with Session(engine) as session:
        assert SourceRepository(session).list_for_user(user.id) == []


def test_create_source_returns_409_on_duplicate(
    client: TestClient, engine: Engine, validator: StubValidator
) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed", title="Example"
        )
        session.commit()
    token = _issue_token(engine, user.id)
    validator.accept(title="Example")
    response = client.post(
        "/api/sources",
        json={"url": "https://blog.example.com/feed"},
        headers=_auth(token),
    )
    assert response.status_code == 409
    with Session(engine) as session:
        assert len(SourceRepository(session).list_for_user(user.id)) == 1


def test_delete_source_returns_204(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed"
        )
        session.commit()
        source_id = source.id
    token = _issue_token(engine, user.id)
    response = client.delete(f"/api/sources/{source_id}", headers=_auth(token))
    assert response.status_code == 204
    assert response.text == ""
    with Session(engine) as session:
        assert SourceRepository(session).list_for_user(user.id) == []


def test_delete_source_cannot_touch_other_users(client: TestClient, engine: Engine) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=owner.id, url="https://blog.example.com/feed"
        )
        session.commit()
        source_id = source.id
    token = _issue_token(engine, intruder.id)
    response = client.delete(f"/api/sources/{source_id}", headers=_auth(token))
    assert response.status_code == 404
    with Session(engine) as session:
        assert len(SourceRepository(session).list_for_user(owner.id)) == 1


# ---------------------------------------------------------------------------
# /api/signals
# ---------------------------------------------------------------------------


def _seed_signals(engine: Engine, user_id: int) -> tuple[int, int]:
    """Create two sources with two signals each and return the source ids."""
    with Session(engine) as session:
        src_repo = SourceRepository(session)
        a = src_repo.create(user_id=user_id, url="https://a.example.com/feed", title="A")
        b = src_repo.create(user_id=user_id, url="https://b.example.com/feed", title="B")
        sig = SignalRepository(session)
        sig.create(
            source_id=a.id,
            guid="a-old",
            title="A-old",
            url="https://a/1",
            published_at=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        )
        sig.create(
            source_id=a.id,
            guid="a-new",
            title="A-new",
            url="https://a/2",
            published_at=datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
        )
        sig.create(
            source_id=b.id,
            guid="b-old",
            title="B-old",
            url="https://b/1",
            published_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
        sig.create(
            source_id=b.id,
            guid="b-new",
            title="B-new",
            url="https://b/2",
            published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )
        session.commit()
        return a.id, b.id


def test_list_signals_orders_by_published_at_desc(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _seed_signals(engine, user.id)
    token = _issue_token(engine, user.id)
    response = client.get("/api/signals", headers=_auth(token))
    assert response.status_code == 200
    titles = [s["title"] for s in response.json()]
    assert titles == ["A-new", "B-new", "A-old", "B-old"]


def test_list_signals_filters_by_owned_source(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    a_id, _ = _seed_signals(engine, user.id)
    token = _issue_token(engine, user.id)
    response = client.get(f"/api/signals?source_id={a_id}", headers=_auth(token))
    assert response.status_code == 200
    titles = {s["title"] for s in response.json()}
    assert titles == {"A-new", "A-old"}


def test_list_signals_returns_empty_for_foreign_source_id(
    client: TestClient, engine: Engine
) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    a_id, _ = _seed_signals(engine, owner.id)
    token = _issue_token(engine, intruder.id)
    response = client.get(f"/api/signals?source_id={a_id}", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == []


def test_list_signals_supports_pagination(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _seed_signals(engine, user.id)
    token = _issue_token(engine, user.id)
    first = client.get("/api/signals?limit=2&offset=0", headers=_auth(token)).json()
    second = client.get("/api/signals?limit=2&offset=2", headers=_auth(token)).json()
    assert [s["title"] for s in first] == ["A-new", "B-new"]
    assert [s["title"] for s in second] == ["A-old", "B-old"]


def test_list_signals_validates_limit(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    token = _issue_token(engine, user.id)
    response = client.get("/api/signals?limit=99999", headers=_auth(token))
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /api/digest/{iso_week}
# ---------------------------------------------------------------------------


def test_get_digest_returns_ranked_sections_for_week(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed", title="Example Blog"
        )
        SignalRepository(session).create(
            source_id=source.id,
            guid="in-1",
            title="Fresh in-window post",
            url="https://blog.example.com/fresh",
            summary="A short summary.",
            published_at=datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
        )
        SignalRepository(session).create(
            source_id=source.id,
            guid="out-1",
            title="Stale pre-window post",
            url="https://blog.example.com/stale",
            summary=None,
            published_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
        session.commit()
    token = _issue_token(engine, user.id)
    # 2026-W30 = Mon 2026-07-20 -> Mon 2026-07-27.
    response = client.get("/api/digest/2026-W30", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["iso_week"] == "2026-W30"
    assert body["week_start"] == "2026-07-20"
    assert body["week_end"] == "2026-07-27"
    assert body["user_email"] == user.email
    assert len(body["sections"]) == 1
    section = body["sections"][0]
    assert section["source_title"] == "Example Blog"
    titles = [item["title"] for item in section["items"]]
    assert titles == ["Fresh in-window post"]
    assert section["items"][0]["summary"] == "A short summary."
    assert section["items"][0]["score"] > 0


def test_get_digest_empty_when_no_signals_in_week(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    token = _issue_token(engine, user.id)
    response = client.get("/api/digest/2026-W30", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["sections"] == []
    assert body["iso_week"] == "2026-W30"


def test_get_digest_404_for_bad_iso_week(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    token = _issue_token(engine, user.id)
    response = client.get("/api/digest/not-a-week", headers=_auth(token))
    assert response.status_code == 404
    response = client.get("/api/digest/2026-W99", headers=_auth(token))
    assert response.status_code == 404


def test_get_digest_scopes_to_authed_user(client: TestClient, engine: Engine) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=owner.id, url="https://blog.example.com/feed", title="Owner Blog"
        )
        SignalRepository(session).create(
            source_id=source.id,
            guid="only-owner",
            title="Owner-only story",
            url="https://blog.example.com/owner",
            published_at=datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
        )
        session.commit()
    token = _issue_token(engine, intruder.id)
    response = client.get("/api/digest/2026-W30", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["sections"] == []
    assert body["user_email"] == intruder.email
