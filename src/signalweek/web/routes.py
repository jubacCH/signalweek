"""Request handlers for the public SignalWeek website.

Every handler reads persisted :class:`Issue` rows and renders one of the
Jinja templates. Missing pages return 404 with a small helper template.

Issues are addressed on the URL as ``YYYY-Www`` (e.g. ``2026-W30``),
which maps to the ``Issue.number`` column (``YYYYWW``) used by the
digest pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from jinja2 import Environment
from markdown_it import MarkdownIt
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from signalweek.db.models import Issue, SignalItem

RouteHandler = Callable[[Request], Awaitable[Response]]

_ISO_WEEK_RE = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")
_MARKDOWN = MarkdownIt("commonmark")


def parse_iso_week(value: str) -> int | None:
    """Return the ``Issue.number`` for ``YYYY-Www`` inputs, else ``None``."""

    match = _ISO_WEEK_RE.match(value)
    if match is None:
        return None
    year = int(match.group("year"))
    week = int(match.group("week"))
    if not (1 <= week <= 53):
        return None
    return year * 100 + week


def format_iso_week(issue: Issue) -> str:
    """Render an :class:`Issue` back to its ``YYYY-Www`` label."""

    year, week = divmod(issue.number, 100)
    return f"{year:04d}-W{week:02d}"


def render_markdown_html(markdown: str) -> str:
    """Render ``markdown`` to HTML using markdown-it (CommonMark)."""

    rendered: str = _MARKDOWN.render(markdown)
    return rendered


MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite drops tzinfo on round-trip; treat naive datetimes as UTC so
    # the JSON payload is timezone-aware regardless of the backend.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def issue_to_json_payload(issue: Issue, items: Sequence[SignalItem]) -> dict[str, object]:
    """Serialise ``issue`` (and its ``items``) into a JSON-ready mapping."""

    return {
        "iso_week": format_iso_week(issue),
        "number": issue.number,
        "title": issue.title,
        "body_markdown": issue.body_markdown,
        "published_at": _iso_or_none(issue.published_at),
        "items": [
            {
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "summary": item.summary,
                "published_at": _iso_or_none(item.published_at),
            }
            for item in items
        ],
    }


@dataclass(frozen=True)
class Handlers:
    """Bundle of the public route handlers."""

    index: RouteHandler
    archive: RouteHandler
    issue: RouteHandler
    issue_markdown: RouteHandler
    issue_json: RouteHandler


def build_routes(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    templates: Environment,
) -> Handlers:
    """Bind the templates + session factory to the route callables."""

    def render(name: str, /, status_code: int = 200, **context: object) -> HTMLResponse:
        template = templates.get_template(name)
        html = template.render(**context)
        return HTMLResponse(html, status_code=status_code)

    def render_not_found(iso_week: str) -> HTMLResponse:
        return render(
            "not_found.html",
            status_code=404,
            title="Not found",
            iso_week=iso_week,
        )

    async def index(request: Request) -> Response:
        del request
        async with session_factory() as session:
            latest = (
                await session.execute(
                    select(Issue).order_by(desc(Issue.number)).limit(1)
                )
            ).scalar_one_or_none()
        if latest is None:
            return render("empty.html", title="SignalWeek")
        return render(
            "issue.html",
            title=latest.title,
            issue=latest,
            iso_week=format_iso_week(latest),
            body_html=render_markdown_html(latest.body_markdown),
        )

    async def archive(request: Request) -> Response:
        del request
        async with session_factory() as session:
            issues = list(
                (
                    await session.execute(
                        select(Issue).order_by(desc(Issue.number))
                    )
                )
                .scalars()
                .all()
            )
        entries = [
            {
                "title": issue.title,
                "iso_week": format_iso_week(issue),
                "published_at": issue.published_at,
            }
            for issue in issues
        ]
        return render("archive.html", title="SignalWeek archive", issues=entries)

    async def issue(request: Request) -> Response:
        iso_week = request.path_params["iso_week"]
        number = parse_iso_week(iso_week)
        if number is None:
            return render_not_found(iso_week)

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Issue).where(Issue.number == number)
                )
            ).scalar_one_or_none()
        if row is None:
            return render_not_found(iso_week)

        return render(
            "issue.html",
            title=row.title,
            issue=row,
            iso_week=format_iso_week(row),
            body_html=render_markdown_html(row.body_markdown),
        )

    async def _load_issue(iso_week: str) -> Issue | None:
        number = parse_iso_week(iso_week)
        if number is None:
            return None
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Issue).where(Issue.number == number)
                )
            ).scalar_one_or_none()

    async def issue_markdown(request: Request) -> Response:
        iso_week = request.path_params["iso_week"]
        row = await _load_issue(iso_week)
        if row is None:
            return PlainTextResponse(
                f"Issue {iso_week} not found\n",
                status_code=404,
                media_type=MARKDOWN_MEDIA_TYPE,
            )
        return PlainTextResponse(row.body_markdown, media_type=MARKDOWN_MEDIA_TYPE)

    async def issue_json(request: Request) -> Response:
        iso_week = request.path_params["iso_week"]
        number = parse_iso_week(iso_week)
        if number is None:
            return JSONResponse({"error": "not found", "iso_week": iso_week}, status_code=404)
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Issue).where(Issue.number == number)
                )
            ).scalar_one_or_none()
            if row is None:
                return JSONResponse(
                    {"error": "not found", "iso_week": iso_week}, status_code=404
                )
            items = list(row.items)
        return JSONResponse(issue_to_json_payload(row, items))

    return Handlers(
        index=index,
        archive=archive,
        issue=issue,
        issue_markdown=issue_markdown,
        issue_json=issue_json,
    )
