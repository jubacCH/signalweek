"""Tests for the GitHub trending ingestion adapter."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import SignalItem
from signalweek.ingest.github import (
    GITHUB_TRENDING_URL,
    ingest_github_trending,
    parse_github_trending,
)

FIXTURE_HTML = """<!DOCTYPE html>
<html>
<body>
  <main>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/foo/bar" class="Link">
          foo /
          <span class="text-normal">bar</span>
        </a>
      </h2>
      <p class="col-9 color-fg-muted my-1 pr-4">
        A cool project
      </p>
      <div class="f6 color-fg-muted mt-2">
        <a href="/foo/bar/stargazers">1,234</a>
      </div>
    </article>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/baz/qux?utm_source=trending">
          baz /
          <span class="text-normal">qux</span>
        </a>
      </h2>
      <p class="col-9 color-fg-muted my-1 pr-4">Another cool project</p>
    </article>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/foo/bar#readme">
          foo /
          <span class="text-normal">bar</span>
        </a>
      </h2>
      <p class="col-9 color-fg-muted my-1 pr-4">Duplicate of first repo</p>
    </article>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/no/description">
          no /
          <span class="text-normal">description</span>
        </a>
      </h2>
    </article>
  </main>
</body>
</html>
"""


def test_parse_github_trending_extracts_repos_and_dedupes() -> None:
    entries = parse_github_trending(FIXTURE_HTML)

    urls = [entry.url for entry in entries]
    assert urls == [
        "https://github.com/foo/bar",
        "https://github.com/baz/qux",
        "https://github.com/no/description",
    ]
    assert entries[0].title == "foo / bar"
    assert entries[1].title == "baz / qux"
    assert entries[2].title == "no / description"
    assert entries[0].summary == "A cool project"
    assert entries[1].summary == "Another cool project"
    assert entries[2].summary is None
    assert all(entry.source == "GitHub Trending" for entry in entries)
    assert all(entry.published_at is None for entry in entries)


def test_parse_github_trending_returns_empty_when_no_articles() -> None:
    assert parse_github_trending("<html><body>No repos here</body></html>") == []
    assert parse_github_trending("") == []


def test_parse_github_trending_accepts_custom_source() -> None:
    entries = parse_github_trending(FIXTURE_HTML, source="GitHub Trending (Python)")
    assert all(entry.source == "GitHub Trending (Python)" for entry in entries)


@respx.mock
async def test_ingest_github_trending_persists_new_items(session: AsyncSession) -> None:
    route = respx.get(GITHUB_TRENDING_URL).mock(
        return_value=httpx.Response(200, text=FIXTURE_HTML)
    )

    async with httpx.AsyncClient() as client:
        result = await ingest_github_trending(session, client=client)

    assert route.called
    call = route.calls.last
    assert call.request.url.params["since"] == "daily"

    assert result.created_count == 3
    assert result.skipped == 0

    rows = (
        await session.execute(select(SignalItem).order_by(SignalItem.url))
    ).scalars().all()
    assert [row.url for row in rows] == [
        "https://github.com/baz/qux",
        "https://github.com/foo/bar",
        "https://github.com/no/description",
    ]
    assert all(row.source == "GitHub Trending" for row in rows)


@respx.mock
async def test_ingest_github_trending_dedupes_against_existing_rows(
    session: AsyncSession,
) -> None:
    session.add(
        SignalItem(
            title="Already here",
            url="https://github.com/foo/bar",
            source="seed",
        )
    )
    await session.commit()

    respx.get(GITHUB_TRENDING_URL).mock(
        return_value=httpx.Response(200, text=FIXTURE_HTML)
    )

    async with httpx.AsyncClient() as client:
        result = await ingest_github_trending(session, client=client)

    assert result.created_count == 2
    assert result.skipped == 1
    created_urls = {item.url for item in result.created}
    assert created_urls == {
        "https://github.com/baz/qux",
        "https://github.com/no/description",
    }

    total = (await session.execute(select(SignalItem))).scalars().all()
    assert len(total) == 3


@respx.mock
async def test_ingest_github_trending_is_idempotent_across_runs(
    session: AsyncSession,
) -> None:
    respx.get(GITHUB_TRENDING_URL).mock(
        return_value=httpx.Response(200, text=FIXTURE_HTML)
    )

    async with httpx.AsyncClient() as client:
        first = await ingest_github_trending(session, client=client)
        second = await ingest_github_trending(session, client=client)

    assert first.created_count == 3
    assert second.created_count == 0
    assert second.skipped == 3


@respx.mock
async def test_ingest_github_trending_raises_on_http_error(session: AsyncSession) -> None:
    respx.get(GITHUB_TRENDING_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ingest_github_trending(session, client=client)

    rows = (await session.execute(select(SignalItem))).scalars().all()
    assert rows == []


@respx.mock
async def test_ingest_github_trending_uses_language_and_since_params(
    session: AsyncSession,
) -> None:
    route = respx.get(f"{GITHUB_TRENDING_URL}/python").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )

    async with httpx.AsyncClient() as client:
        result = await ingest_github_trending(
            session, client=client, language="python", since="weekly"
        )

    assert result.created_count == 0
    assert result.skipped == 0
    assert route.called
    assert route.calls.last.request.url.params["since"] == "weekly"
