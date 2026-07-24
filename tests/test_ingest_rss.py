"""Tests for the RSS ingestion adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import SignalItem
from signalweek.ingest.rss import (
    canonicalize_url,
    ingest_feed,
    parse_feed,
)

FEED_URL = "https://example.com/feed.xml"

FIXTURE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <link>https://example.com/</link>
    <description>Sample feed for tests</description>
    <item>
      <title>First post</title>
      <link>https://example.com/posts/first?utm_source=rss&amp;utm_medium=email</link>
      <description>First entry summary</description>
      <pubDate>Wed, 22 Jul 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Second post</title>
      <link>https://EXAMPLE.com/posts/second#top</link>
      <description>Second entry summary</description>
      <pubDate>Thu, 23 Jul 2026 09:30:00 +0000</pubDate>
    </item>
    <item>
      <title>First post duplicate</title>
      <link>https://example.com/posts/first</link>
      <description>Duplicate</description>
    </item>
  </channel>
</rss>
"""


def test_canonicalize_url_strips_tracking_fragments_and_lowercases_host() -> None:
    assert (
        canonicalize_url("HTTPS://Example.COM/Post?utm_source=x&id=42&fbclid=abc#section")
        == "https://example.com/Post?id=42"
    )


def test_canonicalize_url_gives_empty_path_a_slash() -> None:
    assert canonicalize_url("https://example.com") == "https://example.com/"


def test_parse_feed_dedupes_within_feed() -> None:
    entries = parse_feed(FIXTURE_FEED)

    urls = [entry.url for entry in entries]
    assert urls == [
        "https://example.com/posts/first",
        "https://example.com/posts/second",
    ]
    assert entries[0].source == "Example Feed"
    assert entries[0].published_at == datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    assert entries[1].published_at == datetime(2026, 7, 23, 9, 30, tzinfo=UTC)


@respx.mock
async def test_ingest_feed_persists_new_items(session: AsyncSession) -> None:
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=FIXTURE_FEED,
            headers={"content-type": "application/rss+xml"},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await ingest_feed(FEED_URL, session, client=client)

    assert route.called
    assert result.created_count == 2
    assert result.skipped == 0

    rows = (await session.execute(select(SignalItem).order_by(SignalItem.url))).scalars().all()
    assert [row.url for row in rows] == [
        "https://example.com/posts/first",
        "https://example.com/posts/second",
    ]
    assert all(row.source == "Example Feed" for row in rows)
    assert rows[0].title == "First post"


@respx.mock
async def test_ingest_feed_dedupes_against_existing_rows(
    session: AsyncSession,
) -> None:
    session.add(
        SignalItem(
            title="Already here",
            url="https://example.com/posts/first",
            source="seed",
        )
    )
    await session.commit()

    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=FIXTURE_FEED))

    async with httpx.AsyncClient() as client:
        result = await ingest_feed(FEED_URL, session, client=client)

    assert result.created_count == 1
    assert result.skipped == 1
    assert result.created[0].url == "https://example.com/posts/second"

    total = (await session.execute(select(SignalItem))).scalars().all()
    assert len(total) == 2


@respx.mock
async def test_ingest_feed_is_idempotent_across_runs(session: AsyncSession) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=FIXTURE_FEED))

    async with httpx.AsyncClient() as client:
        first = await ingest_feed(FEED_URL, session, client=client)
        second = await ingest_feed(FEED_URL, session, client=client)

    assert first.created_count == 2
    assert second.created_count == 0
    assert second.skipped == 2


@respx.mock
async def test_ingest_feed_raises_on_http_error(session: AsyncSession) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ingest_feed(FEED_URL, session, client=client)

    rows = (await session.execute(select(SignalItem))).scalars().all()
    assert rows == []
