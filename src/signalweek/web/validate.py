"""Server-side validation of user-supplied feed URLs.

Validation runs two probes against the remote server:

1. **HEAD probe** — a lightweight reachability check that also gives us a
   status code without pulling the whole body. Some servers do not implement
   HEAD (``405 Method Not Allowed`` or ``501 Not Implemented``); those are
   tolerated and we proceed to the GET probe.
2. **Parse probe** — a GET request whose body is fed through :mod:`feedparser`.
   The response is accepted if it yields at least one entry or a feed-level
   title, which weeds out random HTML pages posing as feeds.

All failures are surfaced as :class:`FeedValidationError` with a message that
is safe to render straight back to the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import feedparser
import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0
USER_AGENT = "signalweek-validate/0.1 (+https://signalweek.example)"
MAX_BODY_BYTES = 512 * 1024  # 512 KiB is plenty for a feed document.


class FeedValidationError(ValueError):
    """Raised when a feed URL fails validation."""


@dataclass(frozen=True)
class ValidatedFeed:
    """The outcome of a successful probe."""

    url: str
    title: str | None
    feed_type: str  # "rss" or "atom"


def validate_feed_url(url: str, *, client: httpx.Client | None = None) -> ValidatedFeed:
    """Return a :class:`ValidatedFeed` for ``url`` or raise ``FeedValidationError``.

    A caller-provided ``client`` is used as-is so tests can inject an
    ``httpx.MockTransport``; otherwise a short-lived client with the module
    default timeout is created for the two probes.
    """
    normalized = (url or "").strip()
    if not normalized:
        raise FeedValidationError("Please enter a feed URL.")
    parts = urlsplit(normalized)
    if parts.scheme not in {"http", "https"}:
        raise FeedValidationError("URL must start with http:// or https://.")
    if not parts.netloc:
        raise FeedValidationError("URL is missing a host name.")

    headers = {"User-Agent": USER_AGENT}
    owned = client is None
    active = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        _head_probe(active, normalized, headers)
        body = _get_probe(active, normalized, headers)
    finally:
        if owned:
            active.close()

    parsed = feedparser.parse(body)
    entries = parsed.entries or []
    feed_meta = parsed.feed or {}
    feed_title = feed_meta.get("title") if isinstance(feed_meta, dict) else feed_meta.get("title")
    if not entries and not feed_title:
        raise FeedValidationError(
            "The URL was reachable but did not look like an RSS or Atom feed."
        )
    version = (parsed.get("version") or "").lower()
    feed_type = "atom" if version.startswith("atom") else "rss"
    title = feed_title.strip() if isinstance(feed_title, str) and feed_title.strip() else None
    return ValidatedFeed(url=normalized, title=title, feed_type=feed_type)


def _head_probe(client: httpx.Client, url: str, headers: dict[str, str]) -> None:
    try:
        response = client.head(url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise FeedValidationError(f"Could not reach the URL: {exc}") from exc
    # 405/501 mean HEAD is not implemented — that's fine, the GET probe follows.
    if response.status_code >= 400 and response.status_code not in {405, 501}:
        raise FeedValidationError(f"The URL responded with HTTP {response.status_code}.")


def _get_probe(client: httpx.Client, url: str, headers: dict[str, str]) -> bytes:
    try:
        response = client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FeedValidationError(f"Could not fetch the feed: {exc}") from exc
    return response.content[:MAX_BODY_BYTES]
