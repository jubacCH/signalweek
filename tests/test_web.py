"""Tests for the public SignalWeek web routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from signalweek.db import Issue, SignalItem, create_session_factory
from signalweek.web import build_app
from signalweek.web.routes import format_iso_week, parse_iso_week

WEEK_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def _issue(
    number: int,
    *,
    title: str,
    body: str,
    published_at: datetime | None = None,
    items: list[SignalItem] | None = None,
) -> Issue:
    issue = Issue(
        number=number,
        title=title,
        body_markdown=body,
        published_at=published_at,
    )
    issue.items = items or []
    return issue


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(session_factory=session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestIsoWeekHelpers:
    def test_parse_iso_week_valid(self) -> None:
        assert parse_iso_week("2026-W30") == 202630

    def test_parse_iso_week_pads_single_digit_week(self) -> None:
        assert parse_iso_week("2026-W05") == 202605

    def test_parse_iso_week_rejects_bad_format(self) -> None:
        assert parse_iso_week("2026W30") is None
        assert parse_iso_week("26-W30") is None
        assert parse_iso_week("2026-30") is None
        assert parse_iso_week("garbage") is None

    def test_parse_iso_week_rejects_out_of_range_week(self) -> None:
        assert parse_iso_week("2026-W00") is None
        assert parse_iso_week("2026-W54") is None

    def test_format_iso_week_round_trips(self) -> None:
        issue = Issue(number=202605, title="t", body_markdown="")
        assert format_iso_week(issue) == "2026-W05"


class TestIndexRoute:
    async def test_returns_latest_issue_when_present(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        async with session_factory() as session:
            session.add_all(
                [
                    _issue(
                        202629,
                        title="SignalWeek 2026-W29",
                        body="# Older\n\n- [x](https://x)",
                        published_at=WEEK_START.replace(day=13),
                    ),
                    _issue(
                        202630,
                        title="SignalWeek 2026-W30",
                        body="# Newest\n\n- [Rust](https://rust)",
                        published_at=WEEK_START,
                    ),
                ]
            )
            await session.commit()

        response = await client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "SignalWeek 2026-W30" in body
        # Markdown from the latest issue is rendered as HTML.
        assert '<a href="https://rust">Rust</a>' in body
        # The older issue is not shown on the home page.
        assert "Older" not in body

    async def test_returns_placeholder_when_no_issues(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        assert "No issues published yet" in response.text


class TestArchiveRoute:
    async def test_lists_issues_newest_first(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        async with session_factory() as session:
            session.add_all(
                [
                    _issue(
                        202628,
                        title="SignalWeek 2026-W28",
                        body="body 28",
                        published_at=WEEK_START.replace(day=6),
                    ),
                    _issue(
                        202630,
                        title="SignalWeek 2026-W30",
                        body="body 30",
                        published_at=WEEK_START,
                    ),
                    _issue(
                        202629,
                        title="SignalWeek 2026-W29",
                        body="body 29",
                        published_at=WEEK_START.replace(day=13),
                    ),
                ]
            )
            await session.commit()

        response = await client.get("/issues")
        assert response.status_code == 200
        body = response.text
        # Each week appears as a link to its own page.
        for iso in ("2026-W28", "2026-W29", "2026-W30"):
            assert f'href="/issues/{iso}"' in body

        # Newest first: 30 before 29 before 28.
        pos30 = body.index("2026-W30")
        pos29 = body.index("2026-W29")
        pos28 = body.index("2026-W28")
        assert pos30 < pos29 < pos28

    async def test_empty_archive_shows_placeholder(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues")
        assert response.status_code == 200
        assert "No issues published yet" in response.text


class TestIssueRoute:
    async def test_renders_single_issue_markdown_as_html(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        body_md = (
            "# SignalWeek 2026-W30\n\n"
            "## Rust\n\n"
            "- [Rust ownership](https://one) — Hacker News\n"
        )
        async with session_factory() as session:
            session.add(
                _issue(
                    202630,
                    title="SignalWeek 2026-W30",
                    body=body_md,
                    published_at=WEEK_START,
                )
            )
            await session.commit()

        response = await client.get("/issues/2026-W30")
        assert response.status_code == 200
        html = response.text
        assert "<h1>SignalWeek 2026-W30</h1>" in html
        assert "<h2>Rust</h2>" in html
        assert '<a href="https://one">Rust ownership</a>' in html

    async def test_unknown_week_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues/2026-W30")
        assert response.status_code == 404
        assert "Issue not found" in response.text

    async def test_malformed_iso_week_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues/not-a-week")
        assert response.status_code == 404
        assert "Issue not found" in response.text


class TestMarkdownExport:
    async def test_returns_body_markdown_verbatim_with_markdown_content_type(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        body_md = (
            "# SignalWeek 2026-W30\n\n"
            "## Rust\n\n"
            "- [Rust ownership](https://one) — Hacker News\n"
        )
        async with session_factory() as session:
            session.add(
                _issue(
                    202630,
                    title="SignalWeek 2026-W30",
                    body=body_md,
                    published_at=WEEK_START,
                )
            )
            await session.commit()

        response = await client.get("/issues/2026-W30.md")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        # The raw stored Markdown is returned unmodified — not rendered as HTML.
        assert response.text == body_md
        assert "<h1>" not in response.text

    async def test_unknown_week_returns_404_markdown(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues/2026-W30.md")
        assert response.status_code == 404
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        assert "2026-W30" in response.text

    async def test_malformed_iso_week_returns_404_markdown(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues/not-a-week.md")
        assert response.status_code == 404
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"


class TestJsonExport:
    async def test_returns_issue_payload_with_json_content_type(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        body_md = "# SignalWeek 2026-W30\n\n- [Rust](https://rust)\n"
        published_at = WEEK_START
        item_published = WEEK_START.replace(day=21, hour=12)
        async with session_factory() as session:
            session.add(
                _issue(
                    202630,
                    title="SignalWeek 2026-W30",
                    body=body_md,
                    published_at=published_at,
                    items=[
                        SignalItem(
                            title="Rust ownership",
                            url="https://rust.example/one",
                            source="Hacker News",
                            summary="Ownership explained.",
                            published_at=item_published,
                        ),
                        SignalItem(
                            title="Async in Rust",
                            url="https://rust.example/two",
                            source=None,
                            summary=None,
                            published_at=None,
                        ),
                    ],
                )
            )
            await session.commit()

        response = await client.get("/issues/2026-W30.json")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

        # Payload is valid JSON and matches the stored digest.
        payload = json.loads(response.text)
        assert payload["iso_week"] == "2026-W30"
        assert payload["number"] == 202630
        assert payload["title"] == "SignalWeek 2026-W30"
        assert payload["body_markdown"] == body_md
        assert payload["published_at"] == published_at.isoformat()

        assert len(payload["items"]) == 2
        first = payload["items"][0]
        assert first == {
            "title": "Rust ownership",
            "url": "https://rust.example/one",
            "source": "Hacker News",
            "summary": "Ownership explained.",
            "published_at": item_published.isoformat(),
        }
        second = payload["items"][1]
        assert second["source"] is None
        assert second["summary"] is None
        assert second["published_at"] is None

    async def test_returns_empty_items_when_issue_has_none(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        async with session_factory() as session:
            session.add(
                _issue(
                    202631,
                    title="SignalWeek 2026-W31",
                    body="# empty\n",
                    published_at=WEEK_START.replace(day=27),
                )
            )
            await session.commit()

        response = await client.get("/issues/2026-W31.json")
        assert response.status_code == 200
        payload = response.json()
        assert payload["items"] == []

    async def test_unknown_week_returns_404_json(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues/2026-W30.json")
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        payload = response.json()
        assert payload == {"error": "not found", "iso_week": "2026-W30"}

    async def test_malformed_iso_week_returns_404_json(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/issues/not-a-week.json")
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"] == "not found"

    async def test_html_route_still_wins_over_extensions(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
    ) -> None:
        # Sanity-check that the plain HTML route is unaffected by the new
        # extension-suffixed routes registered before it.
        async with session_factory() as session:
            session.add(
                _issue(
                    202630,
                    title="SignalWeek 2026-W30",
                    body="# hello\n",
                    published_at=WEEK_START,
                )
            )
            await session.commit()

        response = await client.get("/issues/2026-W30")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<h1>hello</h1>" in response.text


class TestStatic:
    async def test_stylesheet_served(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/static/style.css")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")
        assert ".site-header" in response.text


class TestBuildApp:
    def test_requires_session_factory_or_engine(self) -> None:
        with pytest.raises(ValueError):
            build_app()
