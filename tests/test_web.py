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
from signalweek.db.models import User
from signalweek.db.repositories import SourceRepository, UserRepository
from signalweek.db.session import create_session_factory
from signalweek.web import create_app
from signalweek.web.security import hash_password, verify_password
from signalweek.web.sessions import SESSION_COOKIE_NAME, encode_session
from signalweek.web.validate import FeedValidationError, ValidatedFeed


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


class StubValidator:
    """A drop-in replacement for :func:`validate_feed_url` used in tests."""

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
        # Echo the caller-supplied URL so downstream code sees the normalized value.
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
        # Detach so the caller can read attributes after the session closes.
        session.expunge(user)
        return user


def _login(client: TestClient, user_id: int) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, encode_session(user_id))


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


# ---------------------------------------------------------------------------
# Auth (login/logout + session cookie on signup)
# ---------------------------------------------------------------------------


def test_signup_sets_session_cookie(client: TestClient, engine: Engine) -> None:
    response = client.post(
        "/signup",
        data={"email": "grace@example.com", "passphrase": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_accepts_correct_credentials(client: TestClient, engine: Engine) -> None:
    _make_user(engine, "linus@example.com")

    response = client.post(
        "/login",
        data={"email": "linus@example.com", "passphrase": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/sources"
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_rejects_wrong_passphrase(client: TestClient, engine: Engine) -> None:
    _make_user(engine, "linus@example.com")
    response = client.post(
        "/login",
        data={"email": "linus@example.com", "passphrase": "totally-wrong-guess"},
    )
    assert response.status_code == 400
    assert "not recognized" in response.text


def test_logout_clears_cookie(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    # Server sends an empty cookie with Max-Age=0 to clear it.
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie


# ---------------------------------------------------------------------------
# Sources CRUD (HTMX)
# ---------------------------------------------------------------------------


def test_sources_page_requires_authentication(client: TestClient) -> None:
    response = client.get("/sources", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_sources_page_renders_for_authenticated_user(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    response = client.get("/sources")
    assert response.status_code == 200
    body = response.text
    assert "Your sources" in body
    assert 'id="sources-list"' in body
    assert 'id="add-source-form"' in body
    # HTMX script must be loaded so the CRUD interactions work in the browser.
    assert "htmx.min.js" in body


def test_sources_page_lists_existing_sources(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed", title="Example"
        )
        session.commit()
    _login(client, user.id)
    body = client.get("/sources").text
    assert "https://blog.example.com/feed" in body
    assert "Example" in body


def test_add_source_success_returns_form_and_new_item_oob(
    client: TestClient, engine: Engine, validator: StubValidator
) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    validator.accept(title="Example Blog", feed_type="rss")

    response = client.post(
        "/sources",
        data={"url": "https://blog.example.com/feed"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    # Fresh form is the primary swap.
    assert 'id="add-source-form"' in body
    assert 'value=""' in body  # url input cleared
    # New item is out-of-band appended to the list.
    assert 'hx-swap-oob="beforeend:#sources-list"' in body
    assert "Example Blog" in body

    # And it is persisted for the user.
    with Session(engine) as session:
        sources = SourceRepository(session).list_for_user(user.id)
        assert [s.url for s in sources] == ["https://blog.example.com/feed"]
        assert sources[0].title == "Example Blog"
        assert sources[0].type == "rss"


def test_add_source_rejects_invalid_feed(
    client: TestClient, engine: Engine, validator: StubValidator
) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    validator.reject("The URL was reachable but did not look like a feed.")

    response = client.post(
        "/sources",
        data={"url": "https://example.com/nope"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 400
    assert "did not look like a feed" in response.text
    # The URL is preserved so the user can fix it.
    assert "https://example.com/nope" in response.text

    with Session(engine) as session:
        assert SourceRepository(session).list_for_user(user.id) == []


def test_add_source_rejects_duplicate(
    client: TestClient, engine: Engine, validator: StubValidator
) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed", title="Example"
        )
        session.commit()
    _login(client, user.id)
    validator.accept(title="Example")

    response = client.post(
        "/sources",
        data={"url": "https://blog.example.com/feed"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 400
    assert "already in your list" in response.text
    with Session(engine) as session:
        assert len(SourceRepository(session).list_for_user(user.id)) == 1


def test_add_source_requires_authentication(client: TestClient, validator: StubValidator) -> None:
    validator.accept()
    response = client.post(
        "/sources",
        data={"url": "https://blog.example.com/feed"},
        headers={"HX-Request": "true"},
    )
    # HTMX requests get HX-Redirect so the client swaps location.
    assert response.status_code == 401
    assert response.headers.get("hx-redirect") == "/login"


def test_delete_source_removes_row(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed"
        )
        session.commit()
        source_id = source.id

    _login(client, user.id)
    response = client.delete(f"/sources/{source_id}", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert response.text == ""
    with Session(engine) as session:
        assert SourceRepository(session).list_for_user(user.id) == []


def test_delete_source_cannot_touch_other_users_source(client: TestClient, engine: Engine) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=owner.id, url="https://blog.example.com/feed"
        )
        session.commit()
        source_id = source.id

    _login(client, intruder.id)
    response = client.delete(f"/sources/{source_id}", headers={"HX-Request": "true"})
    assert response.status_code == 404
    with Session(engine) as session:
        assert len(SourceRepository(session).list_for_user(owner.id)) == 1


def test_delete_source_requires_authentication(client: TestClient) -> None:
    response = client.delete("/sources/1", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers.get("hx-redirect") == "/login"
