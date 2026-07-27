"""Tests for the RSS/Atom ingest pipeline.

Fetches are exercised through :class:`httpx.MockTransport` fed with on-disk
XML fixtures, so no network I/O runs during the suite.

The pipeline writes into the curated ``raw_items`` table keyed on the active
row it read from ``sources``, so these tests operate purely against the two
SQLAlchemy Core tables defined in :mod:`signalweek.sources`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest import (
    FetchError,
    fetch_feed,
    ingest_all_active,
    ingest_source,
    parse_feed,
)
from signalweek.sources import raw_items_table, sources_metadata, sources_table

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"

FIXED_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    """In-memory SQLite with the ``sources`` and ``raw_items`` tables."""
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_source(
    conn: Connection,
    *,
    url: str,
    kind: str = "rss",
    category_hint: str = "industry_moves",
    active: bool = True,
) -> int:
    result = conn.execute(
        sources_table.insert()
        .values(url=url, kind=kind, category_hint=category_hint, active=active)
        .returning(sources_table.c.id)
    )
    return int(result.scalar_one())


def _stored_rows(conn: Connection, source_id: int) -> list[dict[str, object]]:
    stmt = (
        select(raw_items_table)
        .where(raw_items_table.c.source_id == source_id)
        .order_by(raw_items_table.c.id)
    )
    return [dict(row._mapping) for row in conn.execute(stmt).all()]


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
    assert first.canonical_url == "https://blog.example.com/posts/new-pipeline"
    # The raw ``url`` keeps the original tracking params — canonicalization only
    # runs against ``canonical_url``.
    assert "utm_source=rss" in first.url
    assert first.body == "How we shipped the new ingest pipeline in a week."
    assert first.published_at == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

    assert second.canonical_url == "https://blog.example.com/posts/metrics"
    assert second.published_at == datetime(2026, 7, 21, 12, 30, tzinfo=UTC)


def test_parse_atom_strips_tracking_and_prefers_alternate_link() -> None:
    entries = parse_feed(_fixture("example_atom.xml"))

    assert [e.canonical_url for e in entries] == [
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


def test_ingest_source_persists_new_raw_items(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_fixture("example_rss.xml"))

    with curated_engine.begin() as conn, _mock_client(handler) as client:
        result = ingest_source(
            conn,
            source_id=source_id,
            url="https://blog.example.com/feed",
            client=client,
            now=FIXED_NOW,
        )
        rows = _stored_rows(conn, source_id)

    assert result.inserted == 2
    assert result.skipped == 0
    assert result.error is None
    assert [r["canonical_url"] for r in rows] == [
        "https://blog.example.com/posts/new-pipeline",
        "https://blog.example.com/posts/metrics",
    ]
    assert [r["title"] for r in rows] == [
        "Rolling out the new pipeline",
        "Metrics that matter",
    ]
    # ``url`` stores the raw link (tracking params preserved), ``canonical_url``
    # is the dedup key.
    assert "utm_source=rss" in str(rows[0]["url"])
    naive_now = FIXED_NOW.replace(tzinfo=None)
    for row in rows:
        # SQLite strips the tz info on read; compare against the naive form.
        assert row["fetched_at"] == naive_now
        assert row["first_seen_at"] == naive_now


def test_ingest_source_accepts_prefetched_content_and_skips_network(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://research.example.org/feed.atom")

    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - should not fire
        raise AssertionError("network fetch should not run when content= is provided")

    with curated_engine.begin() as conn, _mock_client(handler) as client:
        result = ingest_source(
            conn,
            source_id=source_id,
            url="https://research.example.org/feed.atom",
            client=client,
            content=_fixture("example_atom.xml"),
            now=FIXED_NOW,
        )

    assert result.inserted == 2


def test_ingest_source_is_idempotent(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://research.example.org/feed.atom")

    content = _fixture("example_atom.xml")

    with curated_engine.begin() as conn:
        first = ingest_source(
            conn,
            source_id=source_id,
            url="https://research.example.org/feed.atom",
            content=content,
            now=FIXED_NOW,
        )
    with curated_engine.begin() as conn:
        second = ingest_source(
            conn,
            source_id=source_id,
            url="https://research.example.org/feed.atom",
            content=content,
            now=FIXED_NOW,
        )
        rows = _stored_rows(conn, source_id)

    assert (first.inserted, first.skipped) == (2, 0)
    assert (second.inserted, second.skipped) == (0, 2)
    assert len(rows) == 2


def test_ingest_source_dedups_new_url_variants_against_existing_rows(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")
        # Pre-seed a raw_item that shares a canonical URL with the fixture's
        # first entry (different casing + tracking params would canonicalize
        # to the same key). The ingest run should not produce a duplicate row.
        conn.execute(
            raw_items_table.insert().values(
                source_id=source_id,
                url="https://blog.example.com/posts/new-pipeline",
                canonical_url="https://blog.example.com/posts/new-pipeline",
                title="Old copy",
                body=None,
                fetched_at=FIXED_NOW,
                first_seen_at=FIXED_NOW,
            )
        )

    with curated_engine.begin() as conn:
        result = ingest_source(
            conn,
            source_id=source_id,
            url="https://blog.example.com/feed",
            content=_fixture("example_rss.xml"),
            now=FIXED_NOW,
        )
        rows = _stored_rows(conn, source_id)

    assert result.inserted == 1
    assert result.skipped == 1
    titles = [r["title"] for r in rows]
    assert titles == ["Old copy", "Metrics that matter"]


def test_ingest_source_same_feed_under_two_source_rows_populates_each(
    curated_engine: Engine,
) -> None:
    """Two source rows pointing at the same feed each get their own raw_items."""
    with curated_engine.begin() as conn:
        source_a = _insert_source(
            conn,
            url="https://research.example.org/feed.atom",
            category_hint="research",
        )
        source_b = _insert_source(
            conn,
            url="https://research.example.org/feed.atom?mirror=1",
            category_hint="research",
        )
    content = _fixture("example_atom.xml")

    with curated_engine.begin() as conn:
        ingest_source(
            conn,
            source_id=source_a,
            url="https://research.example.org/feed.atom",
            content=content,
            now=FIXED_NOW,
        )
        ingest_source(
            conn,
            source_id=source_b,
            url="https://research.example.org/feed.atom?mirror=1",
            content=content,
            now=FIXED_NOW,
        )
        rows_a = _stored_rows(conn, source_a)
        rows_b = _stored_rows(conn, source_b)

    assert len(rows_a) == 2
    assert len(rows_b) == 2


def test_ingest_source_end_to_end_with_mock_transport(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://research.example.org/feed.atom")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://research.example.org/feed.atom"
        return httpx.Response(200, content=_fixture("example_atom.xml"))

    with curated_engine.begin() as conn, _mock_client(handler) as client:
        result = ingest_source(
            conn,
            source_id=source_id,
            url="https://research.example.org/feed.atom",
            client=client,
            now=FIXED_NOW,
        )
        rows = _stored_rows(conn, source_id)

    assert result.inserted == 2
    assert sorted(r["title"] for r in rows) == ["Load shedding in practice", "On latency budgets"]


# ---------------------------------------------------------------------------
# ingest_all_active
# ---------------------------------------------------------------------------


def test_ingest_all_active_visits_every_active_source(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        active_a = _insert_source(conn, url="https://blog.example.com/feed")
        active_b = _insert_source(
            conn,
            url="https://research.example.org/feed.atom",
            kind="atom",
            category_hint="research",
        )
        _insert_source(conn, url="https://retired.example.com/feed", active=False)

    fixtures = {
        "https://blog.example.com/feed": _fixture("example_rss.xml"),
        "https://research.example.org/feed.atom": _fixture("example_atom.xml"),
    }

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        target = str(request.url)
        calls.append(target)
        return httpx.Response(200, content=fixtures[target])

    with curated_engine.begin() as conn, _mock_client(handler) as client:
        run = ingest_all_active(conn, client=client, now=FIXED_NOW)
        rows_a = _stored_rows(conn, active_a)
        rows_b = _stored_rows(conn, active_b)

    assert len(run.per_source) == 2
    assert [r.source_id for r in run.per_source] == [active_a, active_b]
    assert run.total_inserted == 4
    assert run.errors == []
    assert set(calls) == set(fixtures)
    assert "https://retired.example.com/feed" not in calls
    assert len(rows_a) == 2
    assert len(rows_b) == 2


def test_ingest_all_active_captures_per_source_fetch_errors(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        good = _insert_source(conn, url="https://blog.example.com/feed")
        broken = _insert_source(conn, url="https://broken.example.com/feed")

    def handler(request: httpx.Request) -> httpx.Response:
        if "broken.example.com" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, content=_fixture("example_rss.xml"))

    with curated_engine.begin() as conn, _mock_client(handler) as client:
        run = ingest_all_active(conn, client=client, now=FIXED_NOW)
        good_rows = _stored_rows(conn, good)
        broken_rows = _stored_rows(conn, broken)

    assert len(good_rows) == 2
    assert broken_rows == []
    errors = {r.source_id: r for r in run.errors}
    assert set(errors) == {broken}
    assert "503" in (errors[broken].error or "")


def test_ingest_all_active_with_no_active_rows_returns_empty_run(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        run = ingest_all_active(conn, now=FIXED_NOW)

    assert run.per_source == []
    assert run.total_inserted == 0
    assert run.errors == []
