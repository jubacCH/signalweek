"""RSS/Atom ingest into ``raw_items``.

The pipeline is intentionally small and testable:

* :func:`fetch_feed` downloads raw feed bytes with ``httpx``.
* :func:`parse_feed` runs the bytes through ``feedparser`` and returns a
  normalized :class:`FetchedEntry` per item — entries without a usable link
  are discarded and duplicates within one feed collapse on their canonical
  URL.
* :func:`ingest_source` fetches a single active source and writes new
  ``raw_items`` rows for it, dedup'd on ``(source_id, canonical_url)``.
* :func:`ingest_all_active` scans the ``sources`` table for ``active=True``
  rows and runs :func:`ingest_source` for each — this is what a scheduler
  calls once per tick.

The pipeline is source-neutral: every active source is treated the same way,
including any Hacker News feed (which enters as a normal RSS source under
the ``industry_moves`` category, not a first-class fast path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import struct_time
from typing import Any

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest.canonical import canonicalize_url
from signalweek.sources import raw_items_table, sources_table

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "signalweek-ingest/0.1 (+https://signalweek.example)"


class FetchError(RuntimeError):
    """Raised when a feed cannot be fetched from the network."""


@dataclass(frozen=True)
class FetchedEntry:
    """A single feed entry after normalization.

    ``canonical_url`` is the dedup key; ``url`` keeps the raw link so the
    stored row still points at the exact URL the feed published.
    """

    title: str
    url: str
    canonical_url: str
    body: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class SourceIngestResult:
    """Outcome of ingesting a single source."""

    source_id: int
    url: str
    inserted: int
    skipped: int
    error: str | None = None


@dataclass(frozen=True)
class IngestRunResult:
    """Aggregate outcome of :func:`ingest_all_active`."""

    per_source: list[SourceIngestResult] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return sum(r.inserted for r in self.per_source)

    @property
    def total_skipped(self) -> int:
        return sum(r.skipped for r in self.per_source)

    @property
    def errors(self) -> list[SourceIngestResult]:
        return [r for r in self.per_source if r.error is not None]


def fetch_feed(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Download the raw bytes of a feed.

    A caller-provided ``client`` is used as-is so tests can inject a
    ``MockTransport``; otherwise a short-lived client with sensible defaults
    is created for the single request.
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
                title=_entry_title(raw),
                url=link,
                canonical_url=canonical,
                body=_entry_body(raw),
                published_at=_entry_published_at(raw),
            )
        )
    return entries


def ingest_source(
    bind: Session | Connection,
    *,
    source_id: int,
    url: str,
    client: httpx.Client | None = None,
    content: bytes | str | None = None,
    now: datetime | None = None,
) -> SourceIngestResult:
    """Fetch a single source and persist any new items into ``raw_items``.

    Passing ``content`` skips the network fetch and is intended for tests or
    replay from cached bytes. Existing rows are looked up by
    ``(source_id, canonical_url)`` so re-ingesting a feed is idempotent.

    A successful call updates the health counters on the source row via
    :mod:`signalweek.ingest.health`, so the periodic prune step can spot
    dead/silent sources without any manual bookkeeping.
    """
    # Imported here to avoid a circular import: health imports FetchError
    # from this module.
    from signalweek.ingest.health import record_fetch_success, record_items_seen

    raw = content if content is not None else fetch_feed(url, client=client)
    entries = parse_feed(raw)

    connection = _as_connection(bind)
    stamp = now or datetime.now(UTC)

    existing = _existing_canonical_urls(connection, source_id)

    inserted = 0
    skipped = 0
    for entry in entries:
        if entry.canonical_url in existing:
            skipped += 1
            continue
        connection.execute(
            raw_items_table.insert().values(
                source_id=source_id,
                url=entry.url,
                canonical_url=entry.canonical_url,
                title=entry.title,
                body=entry.body,
                fetched_at=stamp,
                first_seen_at=stamp,
            )
        )
        existing.add(entry.canonical_url)
        inserted += 1

    record_fetch_success(connection, source_id=source_id, now=stamp)
    if inserted > 0:
        record_items_seen(connection, source_id=source_id, now=stamp)

    return SourceIngestResult(
        source_id=source_id,
        url=url,
        inserted=inserted,
        skipped=skipped,
    )


def ingest_all_active(
    bind: Session | Connection,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> IngestRunResult:
    """Run one ingest pass over every ``active`` row in ``sources``.

    Errors from individual sources are captured in the returned result rather
    than aborting the whole run — one broken feed should not starve the
    others. Callers that want to react to failures can inspect
    :attr:`IngestRunResult.errors`.
    """
    connection = _as_connection(bind)

    stmt = (
        select(sources_table.c.id, sources_table.c.url)
        .where(sources_table.c.active.is_(True))
        .order_by(sources_table.c.id)
    )
    rows = connection.execute(stmt).all()

    # Imported here to avoid a circular import at module load time.
    from signalweek.ingest.health import record_fetch_failure

    stamp = now or datetime.now(UTC)

    results: list[SourceIngestResult] = []
    for row in rows:
        try:
            results.append(
                ingest_source(
                    connection,
                    source_id=row.id,
                    url=row.url,
                    client=client,
                    now=stamp,
                )
            )
        except FetchError as exc:
            record_fetch_failure(connection, source_id=int(row.id), now=stamp)
            results.append(
                SourceIngestResult(
                    source_id=row.id,
                    url=row.url,
                    inserted=0,
                    skipped=0,
                    error=str(exc),
                )
            )
    return IngestRunResult(per_source=results)


def _existing_canonical_urls(connection: Connection, source_id: int) -> set[str]:
    stmt = select(raw_items_table.c.canonical_url).where(raw_items_table.c.source_id == source_id)
    return set(connection.execute(stmt).scalars().all())


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind


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


def _entry_body(raw: Any) -> str | None:
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
