"""Signal ranking: pure scoring by recency decay + engagement + keyword weights.

The ranking module is deliberately decoupled from the DB layer: callers project
:class:`~signalweek.db.models.Signal` rows (plus any source-provided engagement
metric) into :class:`RankableSignal` and hand a batch to :func:`rank_signals`.
The functions here take no ambient state — ``now`` is passed in explicitly — so
they are trivially unit-testable and safe to reuse from the digest builder,
tests, or ad-hoc scripts.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RankableSignal:
    """A projection of a signal plus its source-provided engagement.

    ``engagement`` is a source-specific popularity number (e.g. Hacker News
    points + comments); RSS-only sources typically leave it at ``0``.
    """

    title: str
    summary: str | None = None
    published_at: datetime | None = None
    engagement: float = 0.0
    id: int | None = None


@dataclass(frozen=True)
class RankingWeights:
    """Tunable weights and knobs for the ranking function."""

    recency: float = 1.0
    engagement: float = 1.0
    keyword: float = 1.0
    half_life_hours: float = 24.0
    engagement_saturation: float = 100.0


DEFAULT_WEIGHTS = RankingWeights()


def recency_score(
    published_at: datetime | None,
    *,
    now: datetime,
    half_life_hours: float,
) -> float:
    """Exponential decay in ``[0, 1]``: ``1`` at ``now``, ``0.5`` at one half-life."""
    if published_at is None or half_life_hours <= 0:
        return 0.0
    age_hours = (now - published_at).total_seconds() / 3600.0
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def engagement_score(engagement: float, *, saturation: float) -> float:
    """Log-compressed engagement in ``[0, 1]``, hitting ``1`` at ``saturation``."""
    if engagement <= 0 or saturation <= 0:
        return 0.0
    return min(math.log1p(engagement) / math.log1p(saturation), 1.0)


def keyword_score(text: str, keywords: Mapping[str, float]) -> float:
    """Sum of weights whose (case-insensitive, whole-word) form appears in ``text``.

    A keyword is counted once regardless of how many times it appears in the
    text, which keeps the score bounded by the user's vocabulary size rather
    than by how noisy an individual item happens to be.
    """
    if not keywords:
        return 0.0
    lowered = text.lower()
    total = 0.0
    for keyword, weight in keywords.items():
        stripped = keyword.strip().lower()
        if not stripped:
            continue
        if re.search(rf"\b{re.escape(stripped)}\b", lowered):
            total += weight
    return total


def score_signal(
    item: RankableSignal,
    *,
    now: datetime,
    weights: RankingWeights = DEFAULT_WEIGHTS,
    keywords: Mapping[str, float] | None = None,
) -> float:
    """Compute the composite score for a single signal."""
    recency = recency_score(item.published_at, now=now, half_life_hours=weights.half_life_hours)
    engagement = engagement_score(item.engagement, saturation=weights.engagement_saturation)
    haystack = item.title if item.summary is None else f"{item.title} {item.summary}"
    keyword = keyword_score(haystack, keywords or {})
    return weights.recency * recency + weights.engagement * engagement + weights.keyword * keyword


@dataclass(frozen=True)
class RankedSignal:
    """One row of a ranked result: the original signal and its composite score."""

    signal: RankableSignal
    score: float
    components: dict[str, float] = field(default_factory=dict)


def rank_signals(
    items: Iterable[RankableSignal],
    *,
    now: datetime,
    weights: RankingWeights = DEFAULT_WEIGHTS,
    keywords: Mapping[str, float] | None = None,
) -> list[RankedSignal]:
    """Return ``items`` scored and sorted by descending score (stable on ties)."""
    ranked: list[RankedSignal] = []
    for item in items:
        recency = recency_score(item.published_at, now=now, half_life_hours=weights.half_life_hours)
        engagement = engagement_score(item.engagement, saturation=weights.engagement_saturation)
        haystack = item.title if item.summary is None else f"{item.title} {item.summary}"
        keyword = keyword_score(haystack, keywords or {})
        total = (
            weights.recency * recency + weights.engagement * engagement + weights.keyword * keyword
        )
        ranked.append(
            RankedSignal(
                signal=item,
                score=total,
                components={
                    "recency": recency,
                    "engagement": engagement,
                    "keyword": keyword,
                },
            )
        )
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked
