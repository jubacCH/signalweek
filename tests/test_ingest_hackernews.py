"""Tests for the Hacker News (Algolia) ingest pipeline.

Fetches are exercised through :class:`httpx.MockTransport` fed with an on-disk
JSON fixture recorded from the shape of the public Algolia response, so no
network I/O runs during the suite.
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
    HackerNewsError,
    fetch_hn,
    ingest_hn_source,
    parse_hn,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hackernews"
HN_QUERY_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,show_hn&hitsPerPage=20"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed_source(session: Session, url: str) -> Source:
    user = UserRepository(session).create(email="tester@example.com", hashed_password="x")
    session.commit()
    source = SourceRepository(session).create(user_id=user.id, url=url, type="hackernews")
    session.commit()
    return source


# ---------------------------------------------------------------------------
# parse_hn
# ---------------------------------------------------------------------------


def test_parse_hn_normalizes_stories_and_skips_comments() -> None:
    hits = parse_hn(_fixture("search_by_date.json"))

    # The comment hit at the end is filtered out; the three stories remain in
    # feed order.
    assert [h.title for h in hits] == [
        "Show HN: A minimal signal aggregator",
        "Rolling out the new pipeline",
        "Metrics that matter",
    ]


def test_parse_hn_uses_hn_item_url_for_self_posts() -> None:
    hits = parse_hn(_fixture("search_by_date.json"))
    show_hn = hits[0]

    assert show_hn.guid == "hn:40000001"
    assert show_hn.url == "https://news.ycombinator.com/item?id=40000001"
    assert show_hn.summary == (
        "I built a small tool that turns RSS feeds into a weekly email digest."
    )
    assert show_hn.published_at == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def test_parse_hn_canonicalizes_outbound_urls() -> None:
    hits = parse_hn(_fixture("search_by_date.json"))
    story, metrics = hits[1], hits[2]

    # Tracking params stripped, casing normalized, trailing slash removed.
    assert story.url == "https://blog.example.com/posts/new-pipeline"
    assert story.guid == "hn:40000002"
    assert metrics.url == "https://blog.example.com/posts/metrics"
    assert metrics.guid == "hn:40000003"


def test_parse_hn_handles_empty_and_malformed_input() -> None:
    assert parse_hn(b"") == []
    assert parse_hn(b'{"hits": []}') == []
    assert parse_hn(b'{"nbHits": 0}') == []
    with pytest.raises(HackerNewsError):
        parse_hn(b"not-json")


# ---------------------------------------------------------------------------
# fetch_hn
# ---------------------------------------------------------------------------


def test_fetch_hn_uses_injected_client_and_returns_bytes() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, content=_fixture("search_by_date.json"))

    with _mock_client(handler) as client:
        body = fetch_hn(HN_QUERY_URL, client=client)

    assert body.startswith(b"{")
    assert seen["req"].url.host == "hn.algolia.com"
    assert seen["req"].headers["accept"] == "application/json"
    assert "signalweek-ingest" in seen["req"].headers["user-agent"]


def test_fetch_hn_raises_on_http_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _mock_client(handler) as client, pytest.raises(HackerNewsError):
        fetch_hn(HN_QUERY_URL, client=client)


# ---------------------------------------------------------------------------
# ingest_hn_source
# ---------------------------------------------------------------------------


def test_ingest_hn_source_persists_new_signals(session: Session) -> None:
    source = _seed_source(session, HN_QUERY_URL)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hn.algolia.com"
        return httpx.Response(200, content=_fixture("search_by_date.json"))

    with _mock_client(handler) as client:
        created = ingest_hn_source(session, source, client=client)
    session.commit()

    assert [s.guid for s in created] == ["hn:40000001", "hn:40000002", "hn:40000003"]
    assert [s.url for s in created] == [
        "https://news.ycombinator.com/item?id=40000001",
        "https://blog.example.com/posts/new-pipeline",
        "https://blog.example.com/posts/metrics",
    ]

    stored = SignalRepository(session).list_for_source(source.id)
    assert {s.guid for s in stored} == {s.guid for s in created}


def test_ingest_hn_source_is_idempotent(session: Session) -> None:
    source = _seed_source(session, HN_QUERY_URL)
    content = _fixture("search_by_date.json")

    first = ingest_hn_source(session, source, content=content)
    session.commit()
    second = ingest_hn_source(session, source, content=content)
    session.commit()

    assert len(first) == 3
    assert second == []
    assert len(SignalRepository(session).list_for_source(source.id)) == 3


def test_ingest_hn_source_dedups_against_existing_rows(session: Session) -> None:
    source = _seed_source(session, HN_QUERY_URL)

    # Pre-seed one signal that matches the guid of the second fixture hit.
    SignalRepository(session).create(
        source_id=source.id,
        guid="hn:40000002",
        title="Old copy",
        url="https://blog.example.com/posts/new-pipeline",
    )
    session.commit()

    created = ingest_hn_source(session, source, content=_fixture("search_by_date.json"))
    session.commit()

    assert [s.guid for s in created] == ["hn:40000001", "hn:40000003"]
    assert len(SignalRepository(session).list_for_source(source.id)) == 3
