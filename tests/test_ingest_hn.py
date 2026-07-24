"""Tests for the Hacker News (Algolia) ingestion adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import SignalItem
from signalweek.ingest.hn import (
    HN_SEARCH_URL,
    ingest_hn,
    parse_hn_hits,
)

QUERY = "llm"

FIXTURE_PAYLOAD: dict[str, Any] = {
    "hits": [
        {
            "objectID": "1001",
            "title": "First story",
            "url": "https://example.com/posts/first?utm_source=hn&utm_medium=algolia",
            "author": "alice",
            "created_at": "2026-07-22T12:00:00.000Z",
            "created_at_i": 1_784_635_200,
            "story_text": None,
        },
        {
            "objectID": "1002",
            "title": "Second story",
            "url": "https://EXAMPLE.com/posts/second#comments",
            "author": "bob",
            "created_at": "2026-07-23T09:30:00.000Z",
            "created_at_i": 1_784_712_600,
        },
        {
            "objectID": "1003",
            "title": "First story duplicate",
            "url": "https://example.com/posts/first",
            "author": "carol",
            "created_at": "2026-07-23T10:00:00.000Z",
            "created_at_i": 1_784_714_400,
        },
        {
            "objectID": "1004",
            "title": "Ask HN: how do you learn?",
            "url": None,
            "author": "dave",
            "story_text": "<p>How do you learn?</p>",
            "created_at": "2026-07-23T11:00:00.000Z",
            "created_at_i": 1_784_718_000,
        },
    ],
    "nbHits": 4,
    "page": 0,
    "nbPages": 1,
    "hitsPerPage": 50,
}


def test_parse_hn_hits_dedupes_within_payload_and_falls_back_to_item_url() -> None:
    entries = parse_hn_hits(FIXTURE_PAYLOAD)

    urls = [entry.url for entry in entries]
    assert urls == [
        "https://example.com/posts/first",
        "https://example.com/posts/second",
        "https://news.ycombinator.com/item?id=1004",
    ]
    assert all(entry.source == "Hacker News" for entry in entries)
    # ``created_at_i`` is preferred over the ISO string.
    assert entries[0].published_at == datetime.fromtimestamp(1_784_635_200, tz=UTC)
    assert entries[2].title == "Ask HN: how do you learn?"
    assert entries[2].summary == "<p>How do you learn?</p>"


def test_parse_hn_hits_falls_back_to_iso_created_at_when_epoch_missing() -> None:
    entries = parse_hn_hits(
        {
            "hits": [
                {
                    "objectID": "42",
                    "title": "Only ISO timestamp",
                    "url": "https://example.com/iso",
                    "created_at": "2026-07-22T12:00:00.000Z",
                }
            ]
        }
    )

    assert len(entries) == 1
    assert entries[0].published_at == datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_parse_hn_hits_returns_empty_when_no_hits() -> None:
    assert parse_hn_hits({"hits": []}) == []
    assert parse_hn_hits({}) == []


@respx.mock
async def test_ingest_hn_persists_new_items(session: AsyncSession) -> None:
    route = respx.get(HN_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=FIXTURE_PAYLOAD)
    )

    async with httpx.AsyncClient() as client:
        result = await ingest_hn(QUERY, session, client=client)

    assert route.called
    call = route.calls.last
    assert call.request.url.params["query"] == QUERY
    assert call.request.url.params["tags"] == "story"

    assert result.created_count == 3
    assert result.skipped == 0

    rows = (await session.execute(select(SignalItem).order_by(SignalItem.url))).scalars().all()
    assert [row.url for row in rows] == [
        "https://example.com/posts/first",
        "https://example.com/posts/second",
        "https://news.ycombinator.com/item?id=1004",
    ]
    assert all(row.source == "Hacker News" for row in rows)


@respx.mock
async def test_ingest_hn_dedupes_against_existing_rows(session: AsyncSession) -> None:
    session.add(
        SignalItem(
            title="Already here",
            url="https://example.com/posts/first",
            source="seed",
        )
    )
    await session.commit()

    respx.get(HN_SEARCH_URL).mock(return_value=httpx.Response(200, json=FIXTURE_PAYLOAD))

    async with httpx.AsyncClient() as client:
        result = await ingest_hn(QUERY, session, client=client)

    assert result.created_count == 2
    assert result.skipped == 1
    created_urls = {entry.url for entry in result.created}
    assert created_urls == {
        "https://example.com/posts/second",
        "https://news.ycombinator.com/item?id=1004",
    }

    total = (await session.execute(select(SignalItem))).scalars().all()
    assert len(total) == 3


@respx.mock
async def test_ingest_hn_is_idempotent_across_runs(session: AsyncSession) -> None:
    respx.get(HN_SEARCH_URL).mock(return_value=httpx.Response(200, json=FIXTURE_PAYLOAD))

    async with httpx.AsyncClient() as client:
        first = await ingest_hn(QUERY, session, client=client)
        second = await ingest_hn(QUERY, session, client=client)

    assert first.created_count == 3
    assert second.created_count == 0
    assert second.skipped == 3


@respx.mock
async def test_ingest_hn_raises_on_http_error(session: AsyncSession) -> None:
    respx.get(HN_SEARCH_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ingest_hn(QUERY, session, client=client)

    rows = (await session.execute(select(SignalItem))).scalars().all()
    assert rows == []


@respx.mock
async def test_ingest_hn_passes_tags_and_hits_per_page(session: AsyncSession) -> None:
    route = respx.get(HN_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"hits": []})
    )

    async with httpx.AsyncClient() as client:
        result = await ingest_hn(
            QUERY,
            session,
            client=client,
            tags="story,front_page",
            hits_per_page=10,
        )

    assert result.created_count == 0
    assert result.skipped == 0
    assert route.called
    params = route.calls.last.request.url.params
    assert params["tags"] == "story,front_page"
    assert params["hitsPerPage"] == "10"
