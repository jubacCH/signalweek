"""Deterministic scoring for weekly digest items.

Ranks items by combining three signals:

* **recency** — exponential decay based on age (half-life configurable).
* **source weight** — per-source multiplier looked up in a table with a
  configurable default for unknown sources.
* **keyword-cluster size** — how many items in the input share at least
  one keyword with this item, normalized to ``[0.0, 1.0]``.

Every function is pure: given the same inputs it returns the same output
and mutates nothing. :func:`rank_items` breaks ties on canonical URL so
the ordering is fully deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

DEFAULT_HALF_LIFE_HOURS = 48.0
DEFAULT_SOURCE_WEIGHT = 0.5
DEFAULT_SOURCE_WEIGHTS: Mapping[str, float] = {
    "Hacker News": 0.9,
    "GitHub Trending": 0.8,
}
DEFAULT_MIX: tuple[float, float, float] = (0.5, 0.3, 0.2)

STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "after",
        "again",
        "all",
        "and",
        "any",
        "are",
        "been",
        "before",
        "being",
        "but",
        "can",
        "did",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "her",
        "here",
        "him",
        "his",
        "how",
        "into",
        "its",
        "just",
        "new",
        "not",
        "our",
        "out",
        "over",
        "she",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "use",
        "used",
        "using",
        "was",
        "were",
        "what",
        "when",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-]{2,}")


@dataclass(frozen=True, slots=True)
class RankingItem:
    """A minimal, hashable input record for the ranker."""

    url: str
    title: str
    source: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ScoredItem:
    """A :class:`RankingItem` together with its scoring breakdown."""

    item: RankingItem
    score: float
    recency: float
    source_weight: float
    cluster: float
    cluster_size: int


def recency_score(
    published_at: datetime | None,
    *,
    now: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> float:
    """Return an exponential-decay score in ``[0.0, 1.0]`` based on age.

    ``1.0`` when ``published_at == now``; halves every ``half_life_hours``.
    Items missing a timestamp, dated in the future, or scored with a
    non-positive half-life all return ``0.0``.
    """

    if published_at is None or half_life_hours <= 0:
        return 0.0
    age_hours = (now - published_at).total_seconds() / 3600.0
    if age_hours < 0:
        return 0.0
    return float(0.5 ** (age_hours / half_life_hours))


def source_score(
    source: str | None,
    weights: Mapping[str, float] = DEFAULT_SOURCE_WEIGHTS,
    *,
    default: float = DEFAULT_SOURCE_WEIGHT,
) -> float:
    """Look ``source`` up in ``weights``, returning ``default`` on miss."""

    if source is None:
        return default
    return weights.get(source, default)


def extract_keywords(
    text: str,
    *,
    stopwords: frozenset[str] = STOPWORDS,
) -> frozenset[str]:
    """Extract lowercased, deduped keywords from ``text``.

    A token is a run of ASCII letters/digits/``+``/``-`` that starts with
    a letter and is at least three characters long. Anything in
    ``stopwords`` (compared lowercased) is dropped.
    """

    return frozenset(match.group(0).lower() for match in _TOKEN_RE.finditer(text)) - stopwords


def cluster_sizes(items: Iterable[RankingItem]) -> list[int]:
    """For each item, count how many items share at least one keyword.

    Every item counts itself, so an item with no shared keywords (or no
    keywords at all) gets a cluster size of ``1``. Order matches ``items``.
    """

    materialized = list(items)
    per_item_keywords = [extract_keywords(item.title) for item in materialized]
    keyword_to_indices: dict[str, set[int]] = {}
    for idx, keywords in enumerate(per_item_keywords):
        for keyword in keywords:
            keyword_to_indices.setdefault(keyword, set()).add(idx)

    sizes: list[int] = []
    for idx, keywords in enumerate(per_item_keywords):
        related: set[int] = {idx}
        for keyword in keywords:
            related.update(keyword_to_indices[keyword])
        sizes.append(len(related))
    return sizes


def cluster_score(size: int, *, total: int) -> float:
    """Normalize a cluster ``size`` to ``[0.0, 1.0]`` given ``total`` items.

    ``size == 1`` (only the item itself) maps to ``0.0``; ``size == total``
    maps to ``1.0``. With one or zero items the score is always ``0.0``.
    """

    if total <= 1:
        return 0.0
    normalized = (size - 1) / (total - 1)
    if normalized < 0.0:
        return 0.0
    if normalized > 1.0:
        return 1.0
    return normalized


def rank_items(
    items: Iterable[RankingItem],
    *,
    now: datetime,
    source_weights: Mapping[str, float] = DEFAULT_SOURCE_WEIGHTS,
    mix: tuple[float, float, float] = DEFAULT_MIX,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    default_source_weight: float = DEFAULT_SOURCE_WEIGHT,
) -> list[ScoredItem]:
    """Score every item and return them sorted by descending score.

    ``mix`` supplies the mixing weights for
    ``(recency, source, cluster)``. Ties on total score break on
    ``item.url`` ascending, so the output is fully deterministic.
    """

    w_recency, w_source, w_cluster = mix
    materialized = list(items)
    sizes = cluster_sizes(materialized)
    total = len(materialized)

    scored: list[ScoredItem] = []
    for item, size in zip(materialized, sizes, strict=True):
        r = recency_score(item.published_at, now=now, half_life_hours=half_life_hours)
        s = source_score(item.source, source_weights, default=default_source_weight)
        c = cluster_score(size, total=total)
        composite = w_recency * r + w_source * s + w_cluster * c
        scored.append(
            ScoredItem(
                item=item,
                score=composite,
                recency=r,
                source_weight=s,
                cluster=c,
                cluster_size=size,
            )
        )

    scored.sort(key=lambda entry: (-entry.score, entry.item.url))
    return scored
