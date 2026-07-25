"""Assemble a :class:`Digest` from a user's signals over a week window.

Two entry points are exposed:

* :func:`assemble_digest` is a pure function that groups, ranks, and trims a
  batch of ``(source, signals)`` pairs into a :class:`Digest`. It has no
  database or network dependency, which keeps it trivial to snapshot-test.
* :func:`build_digest` is a thin wrapper that pulls a user's sources and
  window-scoped signals from a SQLAlchemy session and delegates to
  :func:`assemble_digest`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalweek.db.models import Signal, Source, User
from signalweek.digest.models import Digest, DigestItem, DigestSection
from signalweek.ranking import (
    DEFAULT_WEIGHTS,
    RankableSignal,
    RankingWeights,
    rank_signals,
)

DEFAULT_MAX_ITEMS_PER_SECTION = 5


def assemble_digest(
    *,
    user_email: str,
    window_start: datetime,
    window_end: datetime,
    sources_with_signals: Iterable[tuple[Source, Sequence[Signal]]],
    now: datetime | None = None,
    weights: RankingWeights = DEFAULT_WEIGHTS,
    keywords: Mapping[str, float] | None = None,
    max_items_per_section: int = DEFAULT_MAX_ITEMS_PER_SECTION,
) -> Digest:
    """Group, rank, and trim signals into a ready-to-render :class:`Digest`.

    ``sources_with_signals`` is an iterable of ``(source, signals)`` pairs so
    callers can decide how to partition (usually one entry per source). Signals
    whose ``published_at`` falls outside ``[window_start, window_end)`` are
    dropped; sources that end up with no in-window signals are dropped too.
    Sections are ordered by descending top-item score.
    """
    resolved_now = now if now is not None else window_end
    sections: list[DigestSection] = []
    for source, signals in sources_with_signals:
        section = _build_section(
            source,
            signals,
            now=resolved_now,
            weights=weights,
            keywords=keywords,
            window_start=window_start,
            window_end=window_end,
            max_items=max_items_per_section,
        )
        if section is not None:
            sections.append(section)
    sections.sort(key=lambda s: s.items[0].score, reverse=True)
    return Digest(
        user_email=user_email,
        window_start=window_start,
        window_end=window_end,
        sections=tuple(sections),
    )


def build_digest(
    session: Session,
    user: User,
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
    weights: RankingWeights = DEFAULT_WEIGHTS,
    keywords: Mapping[str, float] | None = None,
    max_items_per_section: int = DEFAULT_MAX_ITEMS_PER_SECTION,
) -> Digest:
    """Query a user's sources + window-scoped signals and assemble their digest."""
    sources = (
        session.execute(select(Source).where(Source.user_id == user.id).order_by(Source.id))
        .scalars()
        .all()
    )
    if not sources:
        return Digest(
            user_email=user.email,
            window_start=window_start,
            window_end=window_end,
            sections=(),
        )
    source_ids = [s.id for s in sources]
    signals = (
        session.execute(
            select(Signal).where(
                Signal.source_id.in_(source_ids),
                Signal.published_at.is_not(None),
                Signal.published_at >= window_start,
                Signal.published_at < window_end,
            )
        )
        .scalars()
        .all()
    )
    by_source: dict[int, list[Signal]] = {s.id: [] for s in sources}
    for sig in signals:
        by_source[sig.source_id].append(sig)
    pairs = [(src, by_source[src.id]) for src in sources]
    return assemble_digest(
        user_email=user.email,
        window_start=window_start,
        window_end=window_end,
        sources_with_signals=pairs,
        now=now,
        weights=weights,
        keywords=keywords,
        max_items_per_section=max_items_per_section,
    )


def _build_section(
    source: Source,
    signals: Sequence[Signal],
    *,
    now: datetime,
    weights: RankingWeights,
    keywords: Mapping[str, float] | None,
    window_start: datetime,
    window_end: datetime,
    max_items: int,
) -> DigestSection | None:
    ws = _as_utc(window_start)
    we = _as_utc(window_end)
    in_window = [
        s for s in signals if s.published_at is not None and ws <= _as_utc(s.published_at) < we
    ]
    if not in_window:
        return None
    rankables = [
        RankableSignal(
            id=s.id,
            title=s.title,
            summary=s.summary,
            published_at=_as_utc(s.published_at),
        )
        for s in in_window
    ]
    ranked = rank_signals(rankables, now=_as_utc(now), weights=weights, keywords=keywords)
    lookup = {s.id: s for s in in_window}
    items = tuple(
        DigestItem(
            title=lookup[r.signal.id].title,
            url=lookup[r.signal.id].url,
            summary=lookup[r.signal.id].summary,
            published_at=lookup[r.signal.id].published_at,
            score=r.score,
        )
        for r in ranked[:max_items]
    )
    return DigestSection(
        source_title=_source_title(source),
        source_url=source.url,
        items=items,
    )


def _source_title(source: Source) -> str:
    if source.title and source.title.strip():
        return source.title.strip()
    return source.url


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime; naive inputs are assumed UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns,
    which would otherwise raise when compared against the tz-aware bounds this
    module receives from the caller.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
