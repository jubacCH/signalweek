"""GitHub trending ingestion adapter.

Scrapes the public ``github.com/trending`` HTML page (no authentication),
normalizes each listed repository into a
:class:`~signalweek.ingest.rss.ParsedEntry`, and persists new items as
:class:`~signalweek.db.SignalItem` rows using the same canonical-URL
deduplication contract as the RSS and Hacker News adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import SignalItem
from signalweek.ingest.rss import (
    IngestResult,
    ParsedEntry,
    canonicalize_url,
)

GITHUB_TRENDING_URL = "https://github.com/trending"

_TITLE_MAX = 300
_SOURCE_MAX = 200
_DEFAULT_SOURCE = "GitHub Trending"
_DEFAULT_SINCE = "daily"


@dataclass(slots=True)
class _RepoScrape:
    url: str | None = None
    title: str | None = None
    description: str | None = None


class _TrendingParser(HTMLParser):
    """Extract repository rows from the GitHub trending HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.repos: list[_RepoScrape] = []
        self._article_depth = 0
        self._current: _RepoScrape | None = None
        self._h2_depth = 0
        self._anchor_open = False
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._desc_depth = 0
        self._desc_parts: list[str] = []

    @staticmethod
    def _has_class(attrs: dict[str, str | None], *needed: str) -> bool:
        classes = (attrs.get("class") or "").split()
        return all(c in classes for c in needed)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if (
            tag == "article"
            and self._article_depth == 0
            and self._has_class(a, "Box-row")
        ):
            self._article_depth = 1
            self._current = _RepoScrape()
            return
        if self._article_depth == 0 or self._current is None:
            return
        if tag == "article":
            self._article_depth += 1
            return
        if self._h2_depth > 0:
            if tag == "h2":
                self._h2_depth += 1
            elif tag == "a" and self._current.url is None and self._anchor_href is None:
                href = a.get("href")
                if href:
                    self._anchor_href = href
                    self._anchor_open = True
            return
        if self._desc_depth > 0:
            if tag == "p":
                self._desc_depth += 1
            return
        if (
            tag == "h2"
            and self._current.url is None
            and self._has_class(a, "lh-condensed")
        ):
            self._h2_depth = 1
        elif (
            tag == "p"
            and self._current.description is None
            and self._has_class(a, "col-9", "color-fg-muted")
        ):
            self._desc_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._article_depth == 0 or self._current is None:
            return
        if self._h2_depth > 0:
            if tag == "a" and self._anchor_open:
                self._anchor_open = False
                if self._anchor_href:
                    self._current.url = self._anchor_href.strip()
                    text = " ".join("".join(self._anchor_parts).split())
                    self._current.title = text or None
                self._anchor_href = None
                self._anchor_parts = []
            if tag == "h2":
                self._h2_depth -= 1
        if self._desc_depth > 0 and tag == "p":
            self._desc_depth -= 1
            if self._desc_depth == 0:
                text = "".join(self._desc_parts).strip()
                self._current.description = text or None
                self._desc_parts = []
        if tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                if self._current.url:
                    self.repos.append(self._current)
                self._current = None
                self._h2_depth = 0
                self._anchor_open = False
                self._anchor_href = None
                self._anchor_parts = []
                self._desc_depth = 0
                self._desc_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor_open:
            self._anchor_parts.append(data)
        elif self._desc_depth > 0:
            self._desc_parts.append(data)


def parse_github_trending(
    html: str,
    *,
    source: str | None = None,
) -> list[ParsedEntry]:
    """Turn GitHub trending HTML into deduped :class:`ParsedEntry` objects.

    Repositories missing a link are dropped. Duplicate canonical URLs within
    the page are collapsed to the first occurrence, mirroring the RSS/HN
    adapter contract.
    """

    parser = _TrendingParser()
    parser.feed(html)
    parser.close()

    feed_source = (source or _DEFAULT_SOURCE)[:_SOURCE_MAX]

    entries: list[ParsedEntry] = []
    seen: set[str] = set()
    for repo in parser.repos:
        href = repo.url
        if not href:
            continue
        absolute = urljoin(GITHUB_TRENDING_URL, href)
        canonical = canonicalize_url(absolute)
        if canonical in seen:
            continue
        seen.add(canonical)

        title = repo.title or canonical
        entries.append(
            ParsedEntry(
                title=title[:_TITLE_MAX],
                url=canonical,
                summary=repo.description,
                published_at=None,
                source=feed_source,
            )
        )
    return entries


async def fetch_github_trending(
    client: httpx.AsyncClient,
    *,
    language: str | None = None,
    since: str = _DEFAULT_SINCE,
) -> str:
    """Fetch the HTML body of the GitHub trending page.

    ``language`` is appended to the URL path (e.g. ``/trending/python``).
    ``since`` is passed as a query parameter (``daily``/``weekly``/``monthly``).
    """

    url = GITHUB_TRENDING_URL
    if language:
        url = f"{url}/{language}"
    response = await client.get(url, params={"since": since}, follow_redirects=True)
    response.raise_for_status()
    return response.text


async def ingest_github_trending(
    session: AsyncSession,
    *,
    client: httpx.AsyncClient | None = None,
    language: str | None = None,
    since: str = _DEFAULT_SINCE,
    source: str | None = None,
) -> IngestResult:
    """Scrape GitHub trending and persist new :class:`SignalItem` rows.

    URLs are canonicalized before comparison so tracking parameters and
    fragments do not cause duplicates. Repositories whose canonical URL
    already exists in the database are counted as skipped rather than
    inserted.
    """

    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()
    try:
        body = await fetch_github_trending(
            http_client, language=language, since=since
        )
    finally:
        if owns_client:
            await http_client.aclose()

    entries = parse_github_trending(body, source=source)
    if not entries:
        return IngestResult()

    urls = [entry.url for entry in entries]
    existing_rows = await session.execute(
        select(SignalItem.url).where(SignalItem.url.in_(urls))
    )
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
