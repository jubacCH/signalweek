"""RSS/Atom ingestion adapter.

Fetches remote feeds with ``httpx``, parses them with ``feedparser``,
canonicalizes entry URLs for deduplication, and persists new items as
:class:`~signalweek.db.SignalItem` rows.
"""

from __future__ import annotations

from calendar import timegm
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from time import struct_time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import SignalItem

_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)
_TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    {"gclid", "fbclid", "mc_cid", "mc_eid", "yclid", "_hsenc", "_hsmi"}
)

_TITLE_MAX = 300
_SOURCE_MAX = 200


def canonicalize_url(url: str) -> str:
    """Return a canonical form of ``url`` suitable for deduplication.

    Lowercases the scheme and host, drops the fragment, and strips common
    tracking query parameters (``utm_*``, ``gclid``, ``fbclid`` and friends).
    """

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    kept_query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.lower().startswith(_TRACKING_PARAM_PREFIXES)
        and name.lower() not in _TRACKING_PARAM_NAMES
    ]
    query = urlencode(kept_query, doseq=True)

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def _to_datetime(value: struct_time | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(timegm(value), tz=UTC)


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(slots=True)
class ParsedEntry:
    """A normalized entry pulled from a parsed feed."""

    title: str
    url: str
    summary: str | None
    published_at: datetime | None
    source: str | None


@dataclass(slots=True)
class IngestResult:
    """Outcome of a single ``ingest_feed`` run."""

    created: list[SignalItem] = field(default_factory=list)
    skipped: int = 0

    @property
    def created_count(self) -> int:
        return len(self.created)


async def fetch_feed(url: str, client: httpx.AsyncClient) -> bytes:
    """Fetch the raw feed body from ``url`` using ``client``."""

    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.content


def parse_feed(data: bytes, *, source: str | None = None) -> list[ParsedEntry]:
    """Parse ``data`` into deduped :class:`ParsedEntry` objects.

    Entries missing a link are dropped. Duplicate canonical URLs within the
    feed are collapsed to the first occurrence.
    """

    # Wrap bytes in BytesIO so feedparser never interprets the input as a URL
    # or filesystem path.
    parsed: Any = feedparser.parse(BytesIO(data))

    feed_source = source
    if feed_source is None:
        feed_source = _coerce_str(getattr(parsed.feed, "title", None))
    if feed_source is not None:
        feed_source = feed_source[:_SOURCE_MAX]

    entries: list[ParsedEntry] = []
    seen: set[str] = set()
    for raw in parsed.entries:
        link = _coerce_str(getattr(raw, "link", None))
        if link is None:
            continue
        canonical = canonicalize_url(link)
        if canonical in seen:
            continue
        seen.add(canonical)

        title = _coerce_str(getattr(raw, "title", None)) or canonical
        summary = _coerce_str(getattr(raw, "summary", None))
        published = _to_datetime(
            getattr(raw, "published_parsed", None) or getattr(raw, "updated_parsed", None)
        )
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


async def ingest_feed(
    feed_url: str,
    session: AsyncSession,
    *,
    client: httpx.AsyncClient | None = None,
    source: str | None = None,
) -> IngestResult:
    """Fetch ``feed_url``, parse it, and persist new :class:`SignalItem` rows.

    URLs are canonicalized before comparison so tracking parameters and
    fragments do not cause duplicates. Items whose canonical URL already
    exists in the database are counted as skipped rather than inserted.
    """

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()
    try:
        body = await fetch_feed(feed_url, http_client)
    finally:
        if owns_client:
            await http_client.aclose()

    entries = parse_feed(body, source=source)
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
