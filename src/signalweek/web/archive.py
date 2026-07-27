"""Read models for the public issue archive.

Two surfaces live here:

* :func:`load_published_issues` — a lightweight, reverse-chronological list of
  every published issue, used by the ``/issues`` index page.
* :func:`load_published_issue_by_week` — the full item payload for a single
  published week, ready to hand to :func:`signalweek.web.renderers.render_issue`
  for the ``/issues/{week_of}`` permalink.

Both loaders are strictly read-only and filter to ``status == 'published'``.
Draft and held issues are editorial state and must never leak onto public
routes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from signalweek.sources import issues_table, items_table


@dataclass(frozen=True)
class PublishedIssueSummary:
    """One row in the ``/issues`` archive index."""

    week_of: date
    published_at: datetime | None
    total_items: int


@dataclass(frozen=True)
class PublishedIssueDetail:
    """Full payload needed to render a single issue's permalink page."""

    week_of: date
    published_at: datetime | None
    items: list[Mapping[str, object]]


def load_published_issues(engine: Engine) -> list[PublishedIssueSummary]:
    """Return every published issue in reverse-chronological order.

    Ordering is by ``week_of`` descending — the newest week appears first —
    with ``id`` as a stable tiebreaker for the unlikely case of two rows
    sharing the same ``week_of``.
    """
    with engine.connect() as conn:
        issue_rows = conn.execute(
            select(
                issues_table.c.id,
                issues_table.c.week_of,
                issues_table.c.published_at,
            )
            .where(issues_table.c.status == "published")
            .order_by(
                issues_table.c.week_of.desc(),
                issues_table.c.id.desc(),
            )
        ).all()

        if not issue_rows:
            return []

        count_rows = conn.execute(
            select(items_table.c.issue_id, func.count(items_table.c.id))
            .where(items_table.c.issue_id.in_([int(r.id) for r in issue_rows]))
            .group_by(items_table.c.issue_id)
        ).all()

    counts = {int(row[0]): int(row[1]) for row in count_rows}
    return [
        PublishedIssueSummary(
            week_of=row.week_of,
            published_at=_ensure_aware(row.published_at),
            total_items=counts.get(int(row.id), 0),
        )
        for row in issue_rows
    ]


def load_published_issue_by_week(engine: Engine, week_of: date) -> PublishedIssueDetail | None:
    """Return the published issue for ``week_of`` with all its items.

    Returns ``None`` when no row exists for that week, or when the row exists
    but is not ``published`` — the caller turns that into a 404. Items carry
    every field the issue renderer needs (``category``, ``position``,
    ``headline``, ``summary``, ``primary_url``, ``extra_source_urls``).
    """
    with engine.connect() as conn:
        issue_row = conn.execute(
            select(
                issues_table.c.id,
                issues_table.c.week_of,
                issues_table.c.status,
                issues_table.c.published_at,
            ).where(issues_table.c.week_of == week_of)
        ).first()

        if issue_row is None or issue_row.status != "published":
            return None

        item_rows = conn.execute(
            select(
                items_table.c.category,
                items_table.c.position,
                items_table.c.headline,
                items_table.c.summary,
                items_table.c.primary_url,
                items_table.c.extra_source_urls,
            )
            .where(items_table.c.issue_id == int(issue_row.id))
            .order_by(items_table.c.position.asc())
        ).all()

    items: list[Mapping[str, object]] = [
        {
            "category": row.category,
            "position": int(row.position),
            "headline": row.headline,
            "summary": row.summary,
            "primary_url": row.primary_url,
            "extra_source_urls": list(row.extra_source_urls or []),
        }
        for row in item_rows
    ]

    return PublishedIssueDetail(
        week_of=issue_row.week_of,
        published_at=_ensure_aware(issue_row.published_at),
        items=items,
    )


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on read — reattach UTC so ``strftime`` is stable."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
