"""Weekly digest assembly: pick, cluster, render, persist.

Given the current instant, :func:`assemble_digest` fetches every
:class:`~signalweek.db.models.SignalItem` whose ``published_at`` falls in
the containing ISO week, ranks them with :mod:`signalweek.ranking`, keeps
the top-N, groups them into keyword clusters, renders a Markdown body,
and persists everything as an :class:`~signalweek.db.models.Issue` row
with the top items reassigned to it.

Every step is deterministic: identical inputs produce identical output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db.models import Issue, SignalItem
from signalweek.ranking import (
    DEFAULT_HALF_LIFE_HOURS,
    DEFAULT_MIX,
    DEFAULT_SOURCE_WEIGHT,
    DEFAULT_SOURCE_WEIGHTS,
    RankingItem,
    ScoredItem,
    extract_keywords,
    rank_items,
)

DEFAULT_TOP_N = 10


@dataclass(frozen=True, slots=True)
class Cluster:
    """A rendered group of :class:`ScoredItem` sharing a keyword."""

    label: str
    entries: tuple[ScoredItem, ...]


def _to_utc(dt: datetime) -> datetime:
    """Return ``dt`` normalized to UTC (naive inputs are assumed UTC)."""

    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return ``[monday, next_monday)`` (UTC) for the ISO week of ``now``."""

    ref = _to_utc(now)
    weekday = ref.isoweekday()  # Monday == 1
    monday_date = ref.date() - timedelta(days=weekday - 1)
    monday = datetime.combine(monday_date, datetime.min.time(), tzinfo=UTC)
    return monday, monday + timedelta(days=7)


def iso_year_week(now: datetime) -> tuple[int, int]:
    """Return ``(iso_year, iso_week)`` for ``now`` (UTC-normalized)."""

    iso_year, iso_week, _ = _to_utc(now).isocalendar()
    return iso_year, iso_week


def issue_number_for(now: datetime) -> int:
    """Return a monotonic per-week issue number, e.g. ``202630``."""

    iso_year, iso_week = iso_year_week(now)
    return iso_year * 100 + iso_week


def group_by_cluster(scored: Sequence[ScoredItem]) -> list[Cluster]:
    """Partition ``scored`` into keyword-connected clusters.

    Two entries share a cluster iff their titles have at least one keyword
    in common (union-find over keyword co-occurrence). Each cluster's
    label is the most-shared keyword within it — ties broken alphabetically
    so the result is deterministic. Clusters sort by their max score
    descending, then by label ascending. Within a cluster, entries keep
    their input order (which is already ranked).
    """

    n = len(scored)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        hi, lo = (ra, rb) if ra > rb else (rb, ra)
        parent[hi] = lo

    keywords_per_item = [extract_keywords(entry.item.title) for entry in scored]
    first_seen: dict[str, int] = {}
    for i, kws in enumerate(keywords_per_item):
        for kw in kws:
            if kw in first_seen:
                union(first_seen[kw], i)
            else:
                first_seen[kw] = i

    grouped: dict[int, list[int]] = {}
    for i in range(n):
        grouped.setdefault(find(i), []).append(i)

    clusters: list[Cluster] = []
    for indices in grouped.values():
        counts: dict[str, int] = {}
        for i in indices:
            for kw in keywords_per_item[i]:
                counts[kw] = counts.get(kw, 0) + 1
        if counts:
            best = max(counts.values())
            label = min(kw for kw, count in counts.items() if count == best)
        else:
            # Singleton with no extractable keyword: fall back to the title.
            label = scored[indices[0]].item.title
        entries = tuple(scored[i] for i in indices)
        clusters.append(Cluster(label=label, entries=entries))

    clusters.sort(key=lambda c: (-max(e.score for e in c.entries), c.label))
    return clusters


def render_markdown(
    clusters: Sequence[Cluster],
    *,
    iso_year: int,
    iso_week: int,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Render ``clusters`` into the Markdown body of an :class:`Issue`."""

    header = f"# SignalWeek {iso_year}-W{iso_week:02d}"
    last_day = (window_end - timedelta(days=1)).date().isoformat()
    intro = (
        f"Curated links from {window_start.date().isoformat()} to {last_day}."
    )

    lines: list[str] = [header, "", intro, ""]
    if not clusters:
        lines.append("_No signals this week._")
    else:
        for cluster in clusters:
            lines.append(f"## {cluster.label.title()}")
            lines.append("")
            for entry in cluster.entries:
                item = entry.item
                suffix = f" — {item.source}" if item.source else ""
                lines.append(f"- [{item.title}]({item.url}){suffix}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def assemble_digest(
    session: AsyncSession,
    *,
    now: datetime,
    top_n: int = DEFAULT_TOP_N,
    source_weights: Mapping[str, float] = DEFAULT_SOURCE_WEIGHTS,
    mix: tuple[float, float, float] = DEFAULT_MIX,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    default_source_weight: float = DEFAULT_SOURCE_WEIGHT,
) -> Issue:
    """Build and persist the digest :class:`Issue` for the week of ``now``.

    Fetches every :class:`SignalItem` published within the containing
    ISO week, ranks them, keeps the top ``top_n``, groups them by keyword
    cluster, renders a Markdown body, and commits the resulting
    :class:`Issue` with the selected items reassigned to it.
    """

    if top_n <= 0:
        raise ValueError("top_n must be positive")

    start, end = iso_week_bounds(now)
    iso_year, iso_week = iso_year_week(now)
    number = iso_year * 100 + iso_week

    stmt = (
        select(SignalItem)
        .where(SignalItem.published_at.is_not(None))
        .where(SignalItem.published_at >= start)
        .where(SignalItem.published_at < end)
    )
    signal_items = list((await session.execute(stmt)).scalars().all())

    # SQLite loses tzinfo on round-trip; coerce back to UTC-aware so the
    # ranker (which subtracts from ``now``) sees consistent datetimes.
    ranking_inputs = [
        RankingItem(
            url=si.url,
            title=si.title,
            source=si.source,
            published_at=_to_utc(si.published_at) if si.published_at else None,
        )
        for si in signal_items
    ]
    scored = rank_items(
        ranking_inputs,
        now=_to_utc(now),
        source_weights=source_weights,
        mix=mix,
        half_life_hours=half_life_hours,
        default_source_weight=default_source_weight,
    )
    top = scored[:top_n]
    clusters = group_by_cluster(top)
    body = render_markdown(
        clusters,
        iso_year=iso_year,
        iso_week=iso_week,
        window_start=start,
        window_end=end,
    )

    by_url = {si.url: si for si in signal_items}
    selected_items = [by_url[entry.item.url] for entry in top]

    issue = Issue(
        number=number,
        title=f"SignalWeek {iso_year}-W{iso_week:02d}",
        body_markdown=body,
        published_at=start,
    )
    issue.items = selected_items
    session.add(issue)
    await session.commit()
    return issue
