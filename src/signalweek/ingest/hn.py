"""Hacker News ingestion adapter.

Queries the public Algolia search API (``hn.algolia.com``), normalizes hits
into :class:`~signalweek.ingest.rss.ParsedEntry` objects, and persists new
items as :class:`~signalweek.db.SignalItem` rows using the same
canonical-URL deduplication contract as the RSS adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import SignalItem
from signalweek.ingest.rss import (
    IngestResult,
    ParsedEntry,
    canonicalize_url,
)

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"

_TITLE_MAX = 300
_SOURCE_MAX = 200
_DEFAULT_SOURCE = "Hacker News"
_DEFAULT_TAGS = "story"
_DEFAULT_HITS_PER_PAGE = 50


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_created_at(hit: dict[str, Any]) -> datetime | None:
    created_i = hit.get("created_at_i")
    if isinstance(created_i, int):
        return datetime.fromtimestamp(created_i, tz=UTC)
    created = hit.get("created_at")
    if isinstance(created, str) and created:
        # Algolia returns ISO-8601 with a trailing ``Z``.
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def fetch_hn_search(
    query: str,
    client: httpx.AsyncClient,
    *,
    tags: str = _DEFAULT_TAGS,
    hits_per_page: int = _DEFAULT_HITS_PER_PAGE,
) -> dict[str, Any]:
    """Fetch a page of results from the Algolia HN search endpoint."""

    params = {
        "query": query,
        "tags": tags,
        "hitsPerPage": str(hits_per_page),
    }
    response = await client.get(HN_SEARCH_URL, params=params)
    response.raise_for_status()
    payload: Any = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected Algolia response: not a JSON object")
    return payload


def parse_hn_hits(
    payload: dict[str, Any],
    *,
    source: str | None = None,
) -> list[ParsedEntry]:
    """Turn an Algolia HN payload into deduped :class:`ParsedEntry` objects.

    Hits without a link fall back to their HN discussion URL so that
    ``Ask HN``-style items still round-trip through the same
    canonical-URL dedupe as the RSS adapter. Duplicates within the same
    payload are collapsed to the first occurrence.
    """

    feed_source = (source or _DEFAULT_SOURCE)[:_SOURCE_MAX]

    hits = payload.get("hits")
    if not isinstance(hits, list):
        return []

    entries: list[ParsedEntry] = []
    seen: set[str] = set()
    for raw in hits:
        if not isinstance(raw, dict):
            continue

        link = _coerce_str(raw.get("url"))
        if link is None:
            object_id = _coerce_str(raw.get("objectID"))
            if object_id is None:
                continue
            link = HN_ITEM_URL.format(id=object_id)

        canonical = canonicalize_url(link)
        if canonical in seen:
            continue
        seen.add(canonical)

        title = (
            _coerce_str(raw.get("title"))
            or _coerce_str(raw.get("story_title"))
            or canonical
        )
        summary = _coerce_str(raw.get("story_text")) or _coerce_str(raw.get("comment_text"))
        published = _parse_created_at(raw)

        entries.append(
            ParsedEntry(
                title=title[:_TITLE_MAX],
                url=canonical,
                summary=summary,
                published_at=published,
                source=feed_source,
            )
        )
    return entries


async def ingest_hn(
    query: str,
    session: AsyncSession,
    *,
    client: httpx.AsyncClient | None = None,
    tags: str = _DEFAULT_TAGS,
    hits_per_page: int = _DEFAULT_HITS_PER_PAGE,
    source: str | None = None,
) -> IngestResult:
    """Query Algolia HN for ``query`` and persist new :class:`SignalItem` rows.

    URLs are canonicalized before comparison so tracking parameters and
    fragments do not cause duplicates. Hits whose canonical URL already
    exists in the database are counted as skipped rather than inserted.
    """

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()
    try:
        payload = await fetch_hn_search(
            query,
            http_client,
            tags=tags,
            hits_per_page=hits_per_page,
        )
    finally:
        if owns_client:
            await http_client.aclose()

    entries = parse_hn_hits(payload, source=source)
    if not entries:
        return IngestResult()

    urls = [entry.url for entry in entries]
    existing_rows = await session.execute(select(SignalItem.url).where(SignalItem.url.in_(urls)))
    existing_urls: set[str] = set(existing_rows.scalars().all())

    result = IngestResult()
    for entry in entries:
        if entry.url in existing_urls:
            result.skipped += 1
            continue
        item = SignalItem(
            title=entry.title,
            url=entry.url,
            summary=entry.summary,
            source=entry.source,
            published_at=entry.published_at,
        )
        session.add(item)
        result.created.append(item)
        existing_urls.add(entry.url)

    if result.created:
        await session.commit()
        for item in result.created:
            await session.refresh(item)
    return result
