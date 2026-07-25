"""End-to-end tests for the FastAPI web layer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from signalweek.db.base import Base
from signalweek.db.models import User
from signalweek.db.repositories import (
    DigestRepository,
    SignalRepository,
    SourceRepository,
    UserRepository,
)
from signalweek.db.session import create_session_factory
from signalweek.web import create_app
from signalweek.web.app import (
    DIGEST_HISTORY_PAGE_SIZE,
    current_week_window,
    format_iso_week,
    parse_iso_week,
)
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


def _make_client(
    engine: Engine,
    validator: StubValidator,
    clock: Callable[[], datetime] | None = None,
) -> TestClient:
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

    app = create_app(session_dependency=_session_dep, feed_validator=validator, clock=clock)
    return TestClient(app)


@pytest.fixture()
def client(engine: Engine, validator: StubValidator) -> Iterator[TestClient]:
    with _make_client(engine, validator) as c:
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


# ---------------------------------------------------------------------------
# Current-week digest view
# ---------------------------------------------------------------------------


# Wednesday 2026-07-22 12:00 UTC — mid-week so the window covers Mon 20 -> Mon 27.
FROZEN_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
FROZEN_WEEK_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
FROZEN_WEEK_END = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture()
def frozen_client(engine: Engine, validator: StubValidator) -> Iterator[TestClient]:
    with _make_client(engine, validator, clock=lambda: FROZEN_NOW) as c:
        yield c


def test_current_week_window_returns_monday_bounded_utc_week() -> None:
    # A Wednesday afternoon lands inside the Mon 20 -> Mon 27 window.
    start, end = current_week_window(datetime(2026, 7, 22, 15, 30, tzinfo=UTC))
    assert start == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 27, 0, 0, tzinfo=UTC)

    # A Monday at 00:00 UTC is the start of its own week.
    start, end = current_week_window(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    assert start == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 27, 0, 0, tzinfo=UTC)

    # A Sunday at 23:59:59 still belongs to the week that started the prior Monday.
    start, end = current_week_window(datetime(2026, 7, 26, 23, 59, 59, tzinfo=UTC))
    assert start == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


def test_digest_requires_authentication(client: TestClient) -> None:
    response = client.get("/digest", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_digest_empty_state_when_user_has_no_signals(
    frozen_client: TestClient, engine: Engine
) -> None:
    user = _make_user(engine)
    _login(frozen_client, user.id)
    response = frozen_client.get("/digest")
    assert response.status_code == 200
    body = response.text
    assert "This week's digest" in body
    # Window is rendered as Mon 2026-07-20 -> Mon 2026-07-27.
    assert "2026-07-20" in body
    assert "2026-07-27" in body
    assert 'id="digest-empty"' in body
    assert "No signals this week" in body


def test_digest_renders_in_window_signals_for_current_user(
    frozen_client: TestClient, engine: Engine
) -> None:
    user = _make_user(engine)
    with Session(engine) as session:
        source = SourceRepository(session).create(
            user_id=user.id, url="https://blog.example.com/feed", title="Example Blog"
        )
        sig_repo = SignalRepository(session)
        sig_repo.create(
            source_id=source.id,
            guid="in-1",
            title="Fresh in-window post",
            url="https://blog.example.com/fresh",
            summary="A summary that shows up in the rendered digest.",
            published_at=datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
        )
        sig_repo.create(
            source_id=source.id,
            guid="out-1",
            title="Stale pre-window post",
            url="https://blog.example.com/stale",
            summary=None,
            published_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
        session.commit()

    _login(frozen_client, user.id)
    response = frozen_client.get("/digest")
    assert response.status_code == 200
    body = response.text
    assert "Example Blog" in body
    assert "Fresh in-window post" in body
    assert "A summary that shows up in the rendered digest." in body
    # Signals outside the [Mon, next Mon) window are excluded.
    assert "Stale pre-window post" not in body
    # The empty-state marker is gone once there is content.
    assert 'id="digest-empty"' not in body


def test_digest_does_not_leak_other_users_signals(
    frozen_client: TestClient, engine: Engine
) -> None:
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
            summary=None,
            published_at=datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
        )
        session.commit()

    _login(frozen_client, intruder.id)
    response = frozen_client.get("/digest")
    assert response.status_code == 200
    assert "Owner-only story" not in response.text
    assert "Owner Blog" not in response.text
    assert "No signals this week" in response.text


# ---------------------------------------------------------------------------
# Digest archive: history list, permalink page, and Markdown download
# ---------------------------------------------------------------------------


def _make_digest_row(engine: Engine, user_id: int, week_start: date, content: str = "") -> None:
    with Session(engine) as session:
        DigestRepository(session).create(
            user_id=user_id, week_start=week_start, content=content or f"body-{week_start}"
        )
        session.commit()


def _seed_signal(
    engine: Engine,
    *,
    user_id: int,
    source_url: str,
    source_title: str,
    guid: str,
    title: str,
    url: str,
    published_at: datetime,
    summary: str | None = None,
) -> None:
    with Session(engine) as session:
        sources = SourceRepository(session).list_for_user(user_id)
        existing = next((s for s in sources if s.url == source_url), None)
        if existing is None:
            existing = SourceRepository(session).create(
                user_id=user_id, url=source_url, title=source_title
            )
        SignalRepository(session).create(
            source_id=existing.id,
            guid=guid,
            title=title,
            url=url,
            summary=summary,
            published_at=published_at,
        )
        session.commit()


def test_parse_iso_week_round_trips() -> None:
    assert parse_iso_week("2026-W29") == date(2026, 7, 13)
    assert format_iso_week(date(2026, 7, 13)) == "2026-W29"
    # An ISO week can span calendar years — check a boundary Monday.
    assert parse_iso_week("2026-W01") == date(2025, 12, 29)
    assert format_iso_week(date(2025, 12, 29)) == "2026-W01"


def test_parse_iso_week_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        parse_iso_week("not-a-week")
    with pytest.raises(ValueError):
        parse_iso_week("2026-29")
    # Week 60 does not exist in 2026.
    with pytest.raises(ValueError):
        parse_iso_week("2026-W60")


def test_digest_history_requires_authentication(client: TestClient) -> None:
    response = client.get("/digest/history", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_digest_history_empty_state(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    response = client.get("/digest/history")
    assert response.status_code == 200
    body = response.text
    assert "Digest archive" in body
    assert 'id="digest-history-empty"' in body


def test_digest_history_lists_digests_newest_first(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _make_digest_row(engine, user.id, date(2026, 7, 6))
    _make_digest_row(engine, user.id, date(2026, 7, 13))
    _make_digest_row(engine, user.id, date(2026, 7, 20))
    _login(client, user.id)
    response = client.get("/digest/history")
    assert response.status_code == 200
    body = response.text
    assert 'id="digest-history-list"' in body
    # Newest week is 2026-W30 (Mon 2026-07-20).
    idx_w30 = body.find("2026-W30")
    idx_w29 = body.find("2026-W29")
    idx_w28 = body.find("2026-W28")
    assert 0 <= idx_w30 < idx_w29 < idx_w28
    # Each row links to the permalink and the .md download.
    assert 'href="/digest/2026-W30"' in body
    assert 'href="/digest/2026-W30.md"' in body


def test_digest_history_paginates(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    # 12 weeks → 2 pages when page size is 10.
    for i in range(DIGEST_HISTORY_PAGE_SIZE + 2):
        _make_digest_row(engine, user.id, date(2026, 1, 5) + _weeks(i))
    _login(client, user.id)

    page1 = client.get("/digest/history").text
    assert "Page 1 of 2" in page1
    assert 'href="/digest/history?page=2"' in page1
    # The oldest week (Jan 5) should not appear on page 1.
    oldest_iso = format_iso_week(date(2026, 1, 5))
    assert oldest_iso not in page1

    page2 = client.get("/digest/history?page=2").text
    assert "Page 2 of 2" in page2
    assert oldest_iso in page2
    assert 'href="/digest/history?page=1"' in page2


def test_digest_history_clamps_out_of_range_page(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _make_digest_row(engine, user.id, date(2026, 7, 13))
    _login(client, user.id)
    response = client.get("/digest/history?page=99")
    assert response.status_code == 200
    assert "2026-W29" in response.text


def test_digest_history_only_shows_current_user_rows(client: TestClient, engine: Engine) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    _make_digest_row(engine, owner.id, date(2026, 7, 13))
    _login(client, intruder.id)
    body = client.get("/digest/history").text
    assert 'id="digest-history-empty"' in body
    assert "2026-W29" not in body


def test_digest_permalink_requires_authentication(client: TestClient) -> None:
    response = client.get("/digest/2026-W29", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_digest_permalink_renders_stored_week(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _make_digest_row(engine, user.id, date(2026, 7, 13))
    _seed_signal(
        engine,
        user_id=user.id,
        source_url="https://blog.example.com/feed",
        source_title="Example Blog",
        guid="archived-1",
        title="Archived story",
        url="https://blog.example.com/story",
        summary="An archived summary.",
        published_at=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    )
    _login(client, user.id)
    response = client.get("/digest/2026-W29")
    assert response.status_code == 200
    body = response.text
    assert "Digest for week 2026-W29" in body
    assert "2026-07-13" in body
    assert "Example Blog" in body
    assert "Archived story" in body
    # Download link + back link are present.
    assert 'href="/digest/2026-W29.md"' in body
    assert 'href="/digest/history"' in body


def test_digest_permalink_404_when_no_stored_row(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    response = client.get("/digest/2026-W29")
    assert response.status_code == 404


def test_digest_permalink_404_for_invalid_iso_week(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    response = client.get("/digest/not-a-week")
    assert response.status_code == 404


def test_digest_permalink_does_not_leak_other_users_digest(
    client: TestClient, engine: Engine
) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    _make_digest_row(engine, owner.id, date(2026, 7, 13))
    _login(client, intruder.id)
    response = client.get("/digest/2026-W29")
    assert response.status_code == 404


def test_digest_markdown_download_requires_authentication(client: TestClient) -> None:
    response = client.get("/digest/2026-W29.md", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_digest_markdown_download_returns_markdown(client: TestClient, engine: Engine) -> None:
    user = _make_user(engine)
    _make_digest_row(engine, user.id, date(2026, 7, 13))
    _seed_signal(
        engine,
        user_id=user.id,
        source_url="https://blog.example.com/feed",
        source_title="Example Blog",
        guid="md-1",
        title="Markdown-ready story",
        url="https://blog.example.com/md",
        summary=None,
        published_at=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    )
    _login(client, user.id)
    response = client.get("/digest/2026-W29.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "signalweek-2026-W29.md" in disposition
    body = response.text
    assert body.startswith("# Signalweek digest")
    assert "Markdown-ready story" in body
    assert "Example Blog" in body
    assert "2026-07-13" in body


def test_digest_markdown_download_404_when_no_stored_row(
    client: TestClient, engine: Engine
) -> None:
    user = _make_user(engine)
    _login(client, user.id)
    response = client.get("/digest/2026-W29.md")
    assert response.status_code == 404


def test_digest_markdown_download_scoped_to_current_user(
    client: TestClient, engine: Engine
) -> None:
    owner = _make_user(engine, "owner@example.com")
    intruder = _make_user(engine, "intruder@example.com")
    _make_digest_row(engine, owner.id, date(2026, 7, 13))
    _login(client, intruder.id)
    response = client.get("/digest/2026-W29.md")
    assert response.status_code == 404


def _weeks(n: int) -> timedelta:
    return timedelta(days=7 * n)
