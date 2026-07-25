"""Hacker News ingest via the Algolia public search API.

The pipeline mirrors :mod:`signalweek.ingest.feeds`:

* :func:`fetch_hn` downloads raw JSON bytes for an Algolia HN query URL with
  ``httpx``.
* :func:`parse_hn` decodes the JSON payload and returns one
  :class:`HackerNewsHit` per story — non-story hits (comments) and rows that
  lack both a ``url`` and an ``objectID`` are discarded.
* :func:`ingest_hn_source` glues the two together and persists new hits as
  :class:`~signalweek.db.models.Signal` rows, deduplicating on the stable
  ``hn:{objectID}`` guid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from signalweek.db.models import Signal, Source
from signalweek.db.repositories import SignalRepository
from signalweek.ingest.canonical import canonicalize_url

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "signalweek-ingest/0.1 (+https://signalweek.example)"
HN_ITEM_URL_TEMPLATE = "https://news.ycombinator.com/item?id={object_id}"


class HackerNewsError(RuntimeError):
    """Raised when the Algolia HN API cannot be fetched or parsed."""


@dataclass(frozen=True)
class HackerNewsHit:
    """A single normalized Algolia HN story hit.

    ``guid`` is ``hn:{objectID}`` so re-ingesting the same story stays
    idempotent even if the linked URL changes later (a common HN moderator
    action). ``url`` is the canonicalized outbound link, or the HN item page
    for self-posts (Ask HN / Show HN with no external URL).
    """

    guid: str
    title: str
    url: str
    summary: str | None
    published_at: datetime | None


def fetch_hn(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Download the raw JSON bytes of an Algolia HN query.

    A caller-provided ``client`` is used as-is so tests can inject a
    ``MockTransport``; otherwise a short-lived client with sensible defaults is
    created for the single request.
    """
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }
    try:
        if client is not None:
            response = client.get(url, headers=headers, follow_redirects=True)
        else:
            with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as owned:
                response = owned.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HackerNewsError(f"failed to fetch HN feed {url!r}: {exc}") from exc
    return response.content


def parse_hn(content: bytes | str) -> list[HackerNewsHit]:
    """Decode an Algolia HN JSON payload and yield normalized story hits."""
    if not content:
        return []
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HackerNewsError(f"invalid JSON payload: {exc}") from exc

    raw_hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(raw_hits, list):
        return []

    hits: list[HackerNewsHit] = []
    seen: set[str] = set()
    for raw in raw_hits:
        if not isinstance(raw, dict):
            continue
        if not _is_story(raw):
            continue
        object_id = _hit_object_id(raw)
        if not object_id:
            continue
        guid = f"hn:{object_id}"
        if guid in seen:
            continue
        url = _hit_url(raw, object_id)
        if not url:
            continue
        title = _hit_title(raw)
        if title is None:
            continue
        seen.add(guid)
        hits.append(
            HackerNewsHit(
                guid=guid,
                title=title,
                url=url,
                summary=_hit_summary(raw),
                published_at=_hit_published_at(raw),
            )
        )
    return hits


def ingest_hn_source(
    session: Session,
    source: Source,
    *,
    client: httpx.Client | None = None,
    content: bytes | str | None = None,
) -> list[Signal]:
    """Fetch ``source`` from the Algolia HN API and persist new signals.

    Passing ``content`` skips the network fetch and is intended for tests or
    replay from cached bytes. Existing signals are looked up by ``hn:{objectID}``
    guid so re-ingesting the same query is idempotent.
    """
    raw = content if content is not None else fetch_hn(source.url, client=client)
    hits = parse_hn(raw)

    repo = SignalRepository(session)
    created: list[Signal] = []
    for hit in hits:
        if repo.get_by_guid(source.id, hit.guid) is not None:
            continue
        signal = repo.create(
            source_id=source.id,
            guid=hit.guid,
            title=hit.title,
            url=hit.url,
            summary=hit.summary,
            published_at=hit.published_at,
        )
        created.append(signal)
    return created


def _is_story(raw: dict[str, Any]) -> bool:
    tags = raw.get("_tags")
    if isinstance(tags, list) and tags:
        # Algolia tags the item type explicitly; only stories become signals.
        return any(tag in {"story", "show_hn", "ask_hn"} for tag in tags if isinstance(tag, str))
    # Fallback: treat as a story if it has a title and no comment_text.
    return bool(raw.get("title")) and not raw.get("comment_text")


def _hit_object_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("objectID")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


def _hit_title(raw: dict[str, Any]) -> str | None:
    title = raw.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    story_title = raw.get("story_title")
    if isinstance(story_title, str) and story_title.strip():
        return story_title.strip()
    return None


def _hit_url(raw: dict[str, Any], object_id: str) -> str | None:
    for key in ("url", "story_url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            canonical = canonicalize_url(value)
            if canonical:
                return canonical
    # Self-posts (Ask HN / Show HN text-only) fall back to the HN item page.
    return canonicalize_url(HN_ITEM_URL_TEMPLATE.format(object_id=object_id))


def _hit_summary(raw: dict[str, Any]) -> str | None:
    for key in ("story_text", "comment_text"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _hit_published_at(raw: dict[str, Any]) -> datetime | None:
    epoch = raw.get("created_at_i")
    if isinstance(epoch, int | float):
        return datetime.fromtimestamp(int(epoch), tz=UTC)
    iso = raw.get("created_at")
    if isinstance(iso, str) and iso.strip():
        # Algolia serializes with a trailing ``Z``; ``fromisoformat`` accepts it
        # in Python 3.11+.
        try:
            parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None
