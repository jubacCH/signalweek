"""End-to-end tests for the FastAPI web layer."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from signalweek.db.base import Base
from signalweek.db.repositories import UserRepository
from signalweek.db.session import create_session_factory
from signalweek.web import create_app
from signalweek.web.security import hash_password, verify_password


@pytest.fixture()
def engine() -> Iterator[Engine]:
    # StaticPool shares a single in-memory SQLite connection across the
    # engine's threads so the schema created below stays visible to the
    # request handler running in a worker thread.
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


@pytest.fixture()
def client(engine: Engine) -> Iterator[TestClient]:
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

    app = create_app(session_dependency=_session_dep)
    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_landing_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Signalweek" in body
    assert "/signup" in body
    # Pico.css is linked from the base template.
    assert "pico.min.css" in body


def test_signup_form_renders(client: TestClient) -> None:
    response = client.get("/signup")
    assert response.status_code == 200
    body = response.text
    assert '<form method="post" action="/signup">' in body
    assert 'name="email"' in body
    assert 'name="passphrase"' in body


def test_pico_css_is_served(client: TestClient) -> None:
    response = client.get("/static/pico.min.css")
    assert response.status_code == 200
    assert "Pico CSS" in response.text


def test_signup_creates_user_and_redirects(client: TestClient, engine: Engine) -> None:
    response = client.post(
        "/signup",
        data={
            "email": "  Ada@Example.com ",
            "passphrase": "correct horse battery staple",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/welcome"

    with Session(engine) as session:
        user = UserRepository(session).get_by_email("ada@example.com")
        assert user is not None
        assert user.email == "ada@example.com"
        assert user.hashed_password.startswith("$argon2")
        assert user.hashed_password != "correct horse battery staple"
        assert verify_password(user.hashed_password, "correct horse battery staple")


def test_signup_rejects_short_passphrase(client: TestClient, engine: Engine) -> None:
    response = client.post(
        "/signup",
        data={"email": "grace@example.com", "passphrase": "short"},
    )
    assert response.status_code == 400
    assert "at least 12 characters" in response.text

    with Session(engine) as session:
        assert UserRepository(session).get_by_email("grace@example.com") is None


def test_signup_rejects_invalid_email(client: TestClient, engine: Engine) -> None:
    response = client.post(
        "/signup",
        data={"email": "not-an-email", "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 400
    assert "valid email" in response.text

    with Session(engine) as session:
        assert UserRepository(session).list() == []


def test_signup_rejects_duplicate_email(client: TestClient, engine: Engine) -> None:
    payload = {
        "email": "linus@example.com",
        "passphrase": "correct horse battery staple",
    }
    first = client.post("/signup", data=payload, follow_redirects=False)
    assert first.status_code == 303

    second = client.post("/signup", data=payload)
    assert second.status_code == 400
    assert "already exists" in second.text

    with Session(engine) as session:
        users = UserRepository(session).list()
        assert len(users) == 1


def test_hash_password_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong passphrase entirely")


def test_hash_password_returns_unique_hashes() -> None:
    a = hash_password("correct horse battery staple")
    b = hash_password("correct horse battery staple")
    assert a != b
