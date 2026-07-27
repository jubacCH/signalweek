"""Data loader for the public landing page.

The landing page shows a one-sentence product intro plus a preview of the
most recent published issue. The preview mirrors the structure of a full
issue page — five fixed categories, headline + primary URL per item — but
without the extra-sources footnote, keeping the page brief.

This module is intentionally a thin read-only view over the ``issues`` /
``items`` tables. It never mutates state, so it can be called from any
request handler without a transactional wrapper.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from signalweek.ingest.classify import CATEGORIES
from signalweek.sources import issues_table, items_table


@dataclass(frozen=True)
class LatestIssuePreview:
    """A compact snapshot of the most recent published issue.

    ``items_by_category`` preserves the fixed five-category order and, within
    each category, ``position`` ascending — the same contract the full issue
    renderer honours.
    """

    issue_id: int
    week_of: date
    published_at: datetime | None
    total_items: int
    items_by_category: Mapping[str, list[Mapping[str, object]]]


def load_latest_published_issue(engine: Engine) -> LatestIssuePreview | None:
    """Return the most recent published issue, or ``None`` if none exists.

    "Most recent" means the greatest ``published_at`` among ``published``
    issues, with the greatest ``id`` used as a tiebreaker. Draft and held
    issues are never returned — the landing page is a public-facing surface
    and must not leak editorial state.
    """
    with engine.connect() as conn:
        issue_row = conn.execute(
            select(
                issues_table.c.id,
                issues_table.c.week_of,
                issues_table.c.published_at,
            )
            .where(issues_table.c.status == "published")
            .order_by(
                issues_table.c.published_at.desc().nulls_last(),
                issues_table.c.id.desc(),
            )
            .limit(1)
        ).first()

        if issue_row is None:
            return None

        item_rows = conn.execute(
            select(
                items_table.c.category,
                items_table.c.position,
                items_table.c.headline,
                items_table.c.summary,
                items_table.c.primary_url,
            )
            .where(items_table.c.issue_id == int(issue_row.id))
            .order_by(items_table.c.position.asc())
        ).all()

    grouped: dict[str, list[Mapping[str, object]]] = {cat: [] for cat in CATEGORIES}
    for row in item_rows:
        if row.category not in grouped:
            continue
        grouped[row.category].append(
            {
                "category": row.category,
                "position": int(row.position),
                "headline": row.headline,
                "summary": row.summary,
                "primary_url": row.primary_url,
            }
        )
    for cat in CATEGORIES:
        grouped[cat].sort(key=lambda it: it["position"])

    return LatestIssuePreview(
        issue_id=int(issue_row.id),
        week_of=issue_row.week_of,
        published_at=_ensure_aware(issue_row.published_at),
        total_items=len(item_rows),
        items_by_category=grouped,
    )


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on read — reattach UTC so ``strftime`` is stable."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
