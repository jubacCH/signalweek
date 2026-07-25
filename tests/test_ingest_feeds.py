"""Tests for the RSS/Atom ingest pipeline.

Fetches are exercised through :class:`httpx.MockTransport` fed with on-disk
XML fixtures, so no network I/O runs during the suite.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from signalweek.db.models import Source
from signalweek.db.repositories import (
    SignalRepository,
    SourceRepository,
    UserRepository,
)
from signalweek.ingest import (
    FetchError,
    fetch_feed,
    ingest_source,
    parse_feed,
)

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed_source(session: Session, url: str) -> Source:
    user = UserRepository(session).create(email="tester@example.com", hashed_password="x")
    session.commit()
    source = SourceRepository(session).create(user_id=user.id, url=url)
    session.commit()
    return source


# ---------------------------------------------------------------------------
# parse_feed
# ---------------------------------------------------------------------------


def test_parse_rss_normalizes_entries_and_dedups_canonical_urls() -> None:
    entries = parse_feed(_fixture("example_rss.xml"))

    # The fourth item has no link, and the third one collapses to the first's
    # canonical URL — so we expect exactly two distinct entries.
    assert [e.title for e in entries] == [
        "Rolling out the new pipeline",
        "Metrics that matter",
    ]

    first, second = entries
    assert first.url == "https://blog.example.com/posts/new-pipeline"
    assert first.guid == first.url
    assert first.summary == "How we shipped the new ingest pipeline in a week."
    assert first.published_at == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

    assert second.url == "https://blog.example.com/posts/metrics"
    assert second.published_at == datetime(2026, 7, 21, 12, 30, tzinfo=UTC)


def test_parse_atom_strips_tracking_and_prefers_alternate_link() -> None:
    entries = parse_feed(_fixture("example_atom.xml"))

    assert [e.url for e in entries] == [
        "https://research.example.org/notes/latency-budgets",
        "https://research.example.org/notes/load-shedding",
    ]
    # ``published`` should win over ``updated`` when both are present.
    assert entries[1].published_at == datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def test_parse_feed_handles_empty_input() -> None:
    assert parse_feed(b"") == []


# ---------------------------------------------------------------------------
# fetch_feed
# ---------------------------------------------------------------------------


def test_fetch_feed_uses_injected_client_and_returns_bytes() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, content=_fixture("example_rss.xml"))

    with _mock_client(handler) as client:
        body = fetch_feed("https://blog.example.com/feed", client=client)

    assert body.startswith(b"<?xml")
    assert seen["req"].url.path == "/feed"
    assert "signalweek-ingest" in seen["req"].headers["user-agent"]


def test_fetch_feed_follows_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://blog.example.com/new"})
        return httpx.Response(200, content=_fixture("example_atom.xml"))

    with _mock_client(handler) as client:
        body = fetch_feed("https://blog.example.com/old", client=client)

    assert b"<feed" in body


def test_fetch_feed_raises_fetcherror_on_http_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _mock_client(handler) as client, pytest.raises(FetchError):
        fetch_feed("https://blog.example.com/feed", client=client)


# ---------------------------------------------------------------------------
# ingest_source
# ---------------------------------------------------------------------------


def test_ingest_source_persists_new_signals(session: Session) -> None:
    source = _seed_source(session, "https://blog.example.com/feed")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_fixture("example_rss.xml"))

    with _mock_client(handler) as client:
        created = ingest_source(session, source, client=client)
    session.commit()

    assert [s.url for s in created] == [
        "https://blog.example.com/posts/new-pipeline",
        "https://blog.example.com/posts/metrics",
    ]
    stored = SignalRepository(session).list_for_source(source.id)
    assert {s.url for s in stored} == {s.url for s in created}


def test_ingest_source_is_idempotent(session: Session) -> None:
    source = _seed_source(session, "https://research.example.org/feed.atom")
    content = _fixture("example_atom.xml")

    first = ingest_source(session, source, content=content)
    session.commit()
    second = ingest_source(session, source, content=content)
    session.commit()

    assert len(first) == 2
    assert second == []
    assert len(SignalRepository(session).list_for_source(source.id)) == 2


def test_ingest_source_dedups_new_url_variants_against_existing_rows(
    session: Session,
) -> None:
    source = _seed_source(session, "https://blog.example.com/feed")

    # Pre-seed a signal that shares a canonical URL with the fixture's first
    # entry (different casing + tracking params). The ingest run should not
    # produce a duplicate row for the same canonical URL.
    SignalRepository(session).create(
        source_id=source.id,
        guid="https://blog.example.com/posts/new-pipeline",
        title="Old copy",
        url="https://blog.example.com/posts/new-pipeline",
    )
    session.commit()

    created = ingest_source(session, source, content=_fixture("example_rss.xml"))
    session.commit()

    assert [s.title for s in created] == ["Metrics that matter"]
    stored = SignalRepository(session).list_for_source(source.id)
    assert len(stored) == 2


def test_ingest_source_supports_multiple_users_with_same_feed(session: Session) -> None:
    """Same feed URL under two Sources produces its own Signal set per source."""
    user_a = UserRepository(session).create(email="a@example.com", hashed_password="x")
    user_b = UserRepository(session).create(email="b@example.com", hashed_password="x")
    session.commit()
    src_repo = SourceRepository(session)
    source_a = src_repo.create(user_id=user_a.id, url="https://research.example.org/feed.atom")
    source_b = src_repo.create(user_id=user_b.id, url="https://research.example.org/feed.atom")
    session.commit()

    content = _fixture("example_atom.xml")
    ingest_source(session, source_a, content=content)
    ingest_source(session, source_b, content=content)
    session.commit()

    assert len(SignalRepository(session).list_for_source(source_a.id)) == 2
    assert len(SignalRepository(session).list_for_source(source_b.id)) == 2


def test_ingest_source_end_to_end_with_mock_transport(session: Session) -> None:
    source = _seed_source(session, "https://research.example.org/feed.atom")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://research.example.org/feed.atom"
        return httpx.Response(200, content=_fixture("example_atom.xml"))

    with _mock_client(handler) as client:
        created = ingest_source(session, source, client=client)
    session.commit()

    titles = sorted(s.title for s in created)
    assert titles == ["Load shedding in practice", "On latency budgets"]
