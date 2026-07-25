"""RSS/Atom feed ingestion.

The pipeline is intentionally small and testable:

* :func:`fetch_feed` downloads raw feed bytes with ``httpx``.
* :func:`parse_feed` runs the bytes through ``feedparser`` and returns a
  normalized :class:`FetchedEntry` per item — entries without a usable link are
  discarded.
* :func:`ingest_source` glues the two together and persists new entries as
  :class:`~signalweek.db.models.Signal` rows, deduplicating on the canonical
  URL both within the incoming batch and against rows already stored for the
  source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any

import feedparser
import httpx
from sqlalchemy.orm import Session

from signalweek.db.models import Signal, Source
from signalweek.db.repositories import SignalRepository
from signalweek.ingest.canonical import canonicalize_url

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "signalweek-ingest/0.1 (+https://signalweek.example)"


class FetchError(RuntimeError):
    """Raised when a feed cannot be fetched from the network."""


@dataclass(frozen=True)
class FetchedEntry:
    """A single feed entry after normalization.

    ``url`` is the canonical form used for dedup; ``guid`` mirrors it so the
    existing ``(source_id, guid)`` uniqueness constraint on :class:`Signal`
    enforces dedup at the database level as well.
    """

    guid: str
    title: str
    url: str
    summary: str | None
    published_at: datetime | None


def fetch_feed(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Download the raw bytes of a feed.

    A caller-provided ``client`` is used as-is so tests can inject a
    ``MockTransport``; otherwise a short-lived client with sensible defaults is
    created for the single request.
    """
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": ("application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"),
    }
    try:
        if client is not None:
            response = client.get(url, headers=headers, follow_redirects=True)
        else:
            with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as owned:
                response = owned.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FetchError(f"failed to fetch feed {url!r}: {exc}") from exc
    return response.content


def parse_feed(content: bytes | str) -> list[FetchedEntry]:
    """Parse raw feed bytes and yield normalized entries."""
    parsed = feedparser.parse(content)
    entries: list[FetchedEntry] = []
    seen: set[str] = set()
    for raw in parsed.entries:
        link = _entry_link(raw)
        if not link:
            continue
        canonical = canonicalize_url(link)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        entries.append(
            FetchedEntry(
                guid=canonical,
                title=_entry_title(raw),
                url=canonical,
                summary=_entry_summary(raw),
                published_at=_entry_published_at(raw),
            )
        )
    return entries


def ingest_source(
    session: Session,
    source: Source,
    *,
    client: httpx.Client | None = None,
    content: bytes | str | None = None,
) -> list[Signal]:
    """Fetch ``source``, parse it, and persist new signals.

    Passing ``content`` skips the network fetch and is intended for tests or
    replay from cached bytes. Existing signals are looked up by canonical URL
    (stored as ``guid``) so re-ingesting a feed is idempotent.
    """
    raw = content if content is not None else fetch_feed(source.url, client=client)
    entries = parse_feed(raw)

    repo = SignalRepository(session)
    created: list[Signal] = []
    for entry in entries:
        if repo.get_by_guid(source.id, entry.guid) is not None:
            continue
        signal = repo.create(
            source_id=source.id,
            guid=entry.guid,
            title=entry.title,
            url=entry.url,
            summary=entry.summary,
            published_at=entry.published_at,
        )
        created.append(signal)
    return created


def _entry_link(raw: Any) -> str | None:
    link = raw.get("link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    # Atom entries may expose alternates via the ``links`` list.
    for candidate in raw.get("links", []) or []:
        if candidate.get("rel", "alternate") == "alternate":
            href = candidate.get("href")
            if isinstance(href, str) and href.strip():
                return href.strip()
    return None


def _entry_title(raw: Any) -> str:
    title = raw.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return "(untitled)"


def _entry_summary(raw: Any) -> str | None:
    for key in ("summary", "description"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _entry_published_at(raw: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = raw.get(key)
        if isinstance(value, struct_time):
            return datetime(*value[:6], tzinfo=UTC)
    return None
