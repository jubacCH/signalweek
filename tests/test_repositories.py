"""Unit tests for the repository layer against SQLite."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signalweek.db.models import Digest, Signal, Source, User
from signalweek.db.repositories import (
    DigestRepository,
    SignalRepository,
    SourceRepository,
    UserRepository,
)


def _make_user(session: Session, email: str = "user@example.com") -> User:
    user = UserRepository(session).create(email=email, hashed_password="hashed")
    session.commit()
    return user


def _make_source(session: Session, user_id: int, url: str) -> Source:
    source = SourceRepository(session).create(user_id=user_id, url=url)
    session.commit()
    return source


def test_user_create_and_lookup(session: Session) -> None:
    repo = UserRepository(session)
    user = repo.create(email="alice@example.com", hashed_password="pwd")
    session.commit()

    assert user.id is not None
    assert user.is_active is True
    assert user.created_at is not None
    assert repo.get(user.id) is user
    fetched = repo.get_by_email("alice@example.com")
    assert fetched is not None and fetched.id == user.id
    assert repo.get_by_email("missing@example.com") is None


def test_user_list_ordered_by_id(session: Session) -> None:
    repo = UserRepository(session)
    a = repo.create(email="a@example.com", hashed_password="x")
    b = repo.create(email="b@example.com", hashed_password="x")
    session.commit()

    assert [u.id for u in repo.list()] == [a.id, b.id]


def test_user_email_is_unique(session: Session) -> None:
    repo = UserRepository(session)
    repo.create(email="dup@example.com", hashed_password="x")
    session.commit()
    with pytest.raises(IntegrityError):
        repo.create(email="dup@example.com", hashed_password="y")
    session.rollback()


def test_source_scoped_lookups(session: Session) -> None:
    u1 = _make_user(session, "u1@example.com")
    u2 = _make_user(session, "u2@example.com")

    repo = SourceRepository(session)
    s1 = repo.create(user_id=u1.id, url="https://a.example.com/feed", title="A")
    repo.create(
        user_id=u2.id,
        url="https://b.example.com/feed",
        title="B",
        is_active=False,
    )
    session.commit()

    listed = repo.list_for_user(u1.id)
    assert [s.id for s in listed] == [s1.id]

    active = repo.list_active()
    assert len(active) == 1 and active[0].id == s1.id


def test_source_url_unique_per_user(session: Session) -> None:
    user = _make_user(session)
    repo = SourceRepository(session)
    repo.create(user_id=user.id, url="https://x.example.com/feed")
    session.commit()
    with pytest.raises(IntegrityError):
        repo.create(user_id=user.id, url="https://x.example.com/feed")
    session.rollback()


def test_source_same_url_allowed_for_different_users(session: Session) -> None:
    u1 = _make_user(session, "one@example.com")
    u2 = _make_user(session, "two@example.com")
    repo = SourceRepository(session)
    repo.create(user_id=u1.id, url="https://shared.example.com/feed")
    repo.create(user_id=u2.id, url="https://shared.example.com/feed")
    session.commit()

    assert len(repo.list_for_user(u1.id)) == 1
    assert len(repo.list_for_user(u2.id)) == 1


def test_signal_dedup_by_guid(session: Session) -> None:
    user = _make_user(session)
    source = _make_source(session, user.id, "https://x.example.com/feed")

    repo = SignalRepository(session)
    original = repo.create(
        source_id=source.id,
        guid="entry-1",
        title="Hello",
        url="https://x.example.com/1",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.commit()

    lookup = repo.get_by_guid(source.id, "entry-1")
    assert lookup is not None and lookup.id == original.id
    assert repo.get_by_guid(source.id, "missing") is None

    with pytest.raises(IntegrityError):
        repo.create(
            source_id=source.id,
            guid="entry-1",
            title="Hello duplicate",
            url="https://x.example.com/1-dup",
        )
    session.rollback()


def test_signal_list_orders_newest_first(session: Session) -> None:
    user = _make_user(session)
    source = _make_source(session, user.id, "https://x.example.com/feed")

    repo = SignalRepository(session)
    repo.create(
        source_id=source.id,
        guid="a",
        title="A",
        url="u1",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.create(
        source_id=source.id,
        guid="b",
        title="B",
        url="u2",
        published_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    repo.create(
        source_id=source.id,
        guid="c",
        title="C",
        url="u3",
        published_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    session.commit()

    guids = [s.guid for s in repo.list_for_source(source.id)]
    assert guids == ["b", "c", "a"]


def test_digest_create_and_lookup_for_week(session: Session) -> None:
    user = _make_user(session)
    repo = DigestRepository(session)
    week = date(2026, 1, 5)
    digest = repo.create(user_id=user.id, week_start=week, content="body")
    session.commit()

    assert digest.id is not None
    got = repo.get(digest.id)
    assert got is not None and got.content == "body"

    weekly = repo.get_for_week(user.id, week)
    assert weekly is not None and weekly.id == digest.id
    assert repo.get_for_week(user.id, date(2026, 1, 12)) is None


def test_digest_unique_per_user_per_week(session: Session) -> None:
    user = _make_user(session)
    repo = DigestRepository(session)
    week = date(2026, 1, 5)
    repo.create(user_id=user.id, week_start=week, content="first")
    session.commit()
    with pytest.raises(IntegrityError):
        repo.create(user_id=user.id, week_start=week, content="second")
    session.rollback()


def test_digest_list_orders_newest_week_first(session: Session) -> None:
    user = _make_user(session)
    repo = DigestRepository(session)
    repo.create(user_id=user.id, week_start=date(2026, 1, 5), content="w1")
    repo.create(user_id=user.id, week_start=date(2026, 1, 19), content="w3")
    repo.create(user_id=user.id, week_start=date(2026, 1, 12), content="w2")
    session.commit()

    weeks = [d.week_start for d in repo.list_for_user(user.id)]
    assert weeks == [date(2026, 1, 19), date(2026, 1, 12), date(2026, 1, 5)]


def test_digest_count_and_paginated_listing(session: Session) -> None:
    user = _make_user(session)
    other = _make_user(session, "other@example.com")
    repo = DigestRepository(session)
    weeks = [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19), date(2026, 1, 26)]
    for i, wk in enumerate(weeks):
        repo.create(user_id=user.id, week_start=wk, content=f"w{i}")
    repo.create(user_id=other.id, week_start=date(2026, 1, 5), content="other")
    session.commit()

    assert repo.count_for_user(user.id) == 4
    assert repo.count_for_user(other.id) == 1

    first_page = repo.list_for_user_paginated(user.id, offset=0, limit=2)
    assert [d.week_start for d in first_page] == [date(2026, 1, 26), date(2026, 1, 19)]

    second_page = repo.list_for_user_paginated(user.id, offset=2, limit=2)
    assert [d.week_start for d in second_page] == [date(2026, 1, 12), date(2026, 1, 5)]

    beyond = repo.list_for_user_paginated(user.id, offset=10, limit=2)
    assert list(beyond) == []


def test_cascade_delete_user_removes_children(session: Session) -> None:
    user = _make_user(session)
    source = _make_source(session, user.id, "https://x.example.com/feed")
    SignalRepository(session).create(source_id=source.id, guid="g", title="T", url="u")
    DigestRepository(session).create(user_id=user.id, week_start=date(2026, 1, 5), content="c")
    session.commit()

    session.delete(user)
    session.commit()

    for model in (User, Source, Signal, Digest):
        count = session.execute(select(func.count()).select_from(model)).scalar_one()
        assert count == 0, f"{model.__tablename__} should be empty after cascade"


def test_relationships_load_from_user(session: Session) -> None:
    user = _make_user(session)
    source = _make_source(session, user.id, "https://x.example.com/feed")
    SignalRepository(session).create(source_id=source.id, guid="g", title="T", url="u")
    DigestRepository(session).create(user_id=user.id, week_start=date(2026, 1, 5), content="c")
    session.commit()

    session.expire_all()
    refreshed = session.get(User, user.id)
    assert refreshed is not None
    assert [s.url for s in refreshed.sources] == [source.url]
    assert [d.content for d in refreshed.digests] == ["c"]
    assert [sig.guid for sig in refreshed.sources[0].signals] == ["g"]
