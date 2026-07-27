"""Score and order clusters into per-category lists for the weekly digest.

Ranking is a pure product of four bounded factors:

* **Source authority** — the highest domain-authority weight across all of a
  cluster's raw_items. Domains not in the bundled table fall back to a neutral
  default.
* **Recency** — exponential decay against ``now`` of the cluster's *earliest*
  ``first_seen_at`` (when the story broke, not when the last mirror surfaced).
* **Cross-source multiplicity** — ``1 + log2(distinct_sources)``. A story
  picked up by many outlets outranks a lone-blog scoop of similar recency.
* **Category signal** — per-category multiplier driven by lexical or numeric
  cues in the headline (dollar amount for funding, release verbs for models,
  court/regulator cues for lawsuits, etc.).

There is deliberately no per-user personalisation — the digest is a single
editorial artefact and every reader sees the same ordering. Ties break by
earliest ``first_seen_at`` (older stories float up) then by ``cluster_id`` for
a fully deterministic result.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.classify import CATEGORIES, FALLBACK_CATEGORY
from signalweek.sources import clusters_table, raw_items_table, sources_table

# Neutral fallback for any source whose host is not in the bundled table.
DEFAULT_AUTHORITY: float = 0.5

# Per-domain editorial weights. Primary sources (labs, regulators, arXiv) get
# the top of the scale; established reporting sits just below; community
# aggregators sit at the bottom. Domains not listed fall back to
# ``DEFAULT_AUTHORITY`` — the table is a *whitelist*, not an exhaustive index.
_SOURCE_AUTHORITY: dict[str, float] = {
    # Frontier labs — first-hand model / product news.
    "openai.com": 1.0,
    "anthropic.com": 1.0,
    "deepmind.google": 1.0,
    "ai.meta.com": 1.0,
    "mistral.ai": 1.0,
    "x.ai": 1.0,
    "huggingface.co": 0.9,
    # Regulators, courts, and government policy.
    "ftc.gov": 1.0,
    "whitehouse.gov": 1.0,
    "justice.gov": 1.0,
    "sec.gov": 1.0,
    "digital-strategy.ec.europa.eu": 1.0,
    "europa.eu": 0.95,
    # Research.
    "arxiv.org": 0.95,
    "research.google": 0.9,
    "hai.stanford.edu": 0.85,
    # Established reporting.
    "nytimes.com": 0.9,
    "wsj.com": 0.9,
    "ft.com": 0.9,
    "bloomberg.com": 0.9,
    "reuters.com": 0.9,
    "theinformation.com": 0.9,
    "techcrunch.com": 0.75,
    "theverge.com": 0.75,
    "wired.com": 0.75,
    "arstechnica.com": 0.75,
    # Community aggregators.
    "news.ycombinator.com": 0.55,
    "reddit.com": 0.4,
}


@dataclass(frozen=True)
class ClusterSource:
    """A single source feeding a cluster, projected to the fields ranking needs.

    ``source_url`` is the registry URL of the source (e.g.
    ``https://openai.com/blog/rss.xml``), not the article URL — authority and
    multiplicity are both attributed to the *outlet*, not the linked page.
    """

    source_url: str
    first_seen_at: datetime


@dataclass(frozen=True)
class ClusterInput:
    """A cluster plus the sources whose raw_items feed it."""

    id: int
    category: str
    canonical_headline: str
    primary_url: str
    sources: tuple[ClusterSource, ...]


@dataclass(frozen=True)
class RankingWeights:
    """Tunable knobs for the ranking function."""

    recency_half_life_hours: float = 72.0
    default_authority: float = DEFAULT_AUTHORITY


DEFAULT_WEIGHTS = RankingWeights()


@dataclass(frozen=True)
class RankedCluster:
    """A cluster with its total score and the four contributing factors.

    Components are exposed so tests and admin views can explain a ranking
    without recomputing it.
    """

    cluster_id: int
    category: str
    canonical_headline: str
    primary_url: str
    score: float
    authority: float
    recency: float
    multiplicity: float
    category_signal: float


# ---------------------------------------------------------------------------
# Component scores
# ---------------------------------------------------------------------------


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def authority_for_url(url: str, *, default: float = DEFAULT_AUTHORITY) -> float:
    """Return the authority weight for ``url``'s host.

    Falls back to ``default`` when the host is unknown. Subdomains inherit
    their parent domain's weight — e.g. ``blog.openai.com`` falls back to
    ``openai.com`` before defaulting.
    """
    host = _host(url)
    if not host:
        return default
    if host in _SOURCE_AUTHORITY:
        return _SOURCE_AUTHORITY[host]
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in _SOURCE_AUTHORITY:
            return _SOURCE_AUTHORITY[candidate]
    return default


def cluster_authority(
    sources: Iterable[ClusterSource], *, default: float = DEFAULT_AUTHORITY
) -> float:
    """Best (max) authority across ``sources``. Empty input returns ``default``."""
    scores = [authority_for_url(s.source_url, default=default) for s in sources]
    return max(scores) if scores else default


def recency_score(
    first_seen_at: datetime | None,
    *,
    now: datetime,
    half_life_hours: float,
) -> float:
    """Exponential decay in ``(0, 1]``: ``1`` at ``now``, ``0.5`` at one half-life.

    Returns ``0`` for a missing timestamp or a non-positive half-life, and
    ``1`` for a story dated in the future (clock skew).
    """
    if first_seen_at is None or half_life_hours <= 0:
        return 0.0
    age_hours = (now - first_seen_at).total_seconds() / 3600.0
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def multiplicity_score(sources: Iterable[ClusterSource]) -> float:
    """``1 + log2(distinct_source_urls)``. One source = 1.0, two = 2.0, four = 3.0."""
    unique = {s.source_url for s in sources if s.source_url}
    n = len(unique)
    if n <= 0:
        return 1.0
    return 1.0 + math.log2(n)


# ---------------------------------------------------------------------------
# Category-specific signals
# ---------------------------------------------------------------------------

# ``$60B``, ``$1.5M``, ``$500k`` — number followed by a magnitude suffix.
_DOLLAR_SUFFIX_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*([kmbt])(?![a-z])",
    re.IGNORECASE,
)
# ``$60 billion``, ``$500 million``.
_DOLLAR_WORD_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*(thousand|million|billion|trillion)\b",
    re.IGNORECASE,
)

_WORD_MULTIPLIER: dict[str, float] = {
    "thousand": 1e3,
    "million": 1e6,
    "billion": 1e9,
    "trillion": 1e12,
}
_SUFFIX_MULTIPLIER: dict[str, float] = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}

_MODEL_RELEASE_VERB_RE = re.compile(
    r"\b(unveil(?:s|ed)?|launch(?:es|ed)?|releas(?:e|es|ed)|"
    r"ships?|shipped|announc(?:es|ed)?|introduc(?:es|ed)?|debut(?:s|ed)?)\b",
    re.IGNORECASE,
)
_MODEL_NAME_RE = re.compile(
    r"\b(gpt(?:-?\d+(?:\.\d+)?)?|claude(?:\s*\d+(?:\.\d+)?)?|"
    r"gemini(?:\s*\d+(?:\.\d+)?)?|llama(?:\s*\d+(?:\.\d+)?)?|"
    r"mistral|mixtral|grok|phi|qwen|deepseek)\b",
    re.IGNORECASE,
)

_LAWSUIT_HIGH_RE = re.compile(
    r"\b(executive order|supreme court|court rules?|ruling|class action|"
    r"antitrust|settlement|fined|subpoena|injunction)\b",
    re.IGNORECASE,
)

_RESEARCH_HIGH_RE = re.compile(
    r"\b(state-of-the-art|sota|novel|benchmark|preprint|arxiv)\b",
    re.IGNORECASE,
)

_INDUSTRY_HIGH_RE = re.compile(
    r"\b(ceo|cto|cfo|coo|chief\s+\w+\s+officer|layoffs?|"
    r"restructur(?:e|es|ing)|acquires?)\b",
    re.IGNORECASE,
)


def _parse_max_dollar_amount(text: str) -> float | None:
    """Return the largest USD amount found in ``text`` (in dollars) or ``None``."""
    best: float | None = None
    for m in _DOLLAR_SUFFIX_RE.finditer(text):
        amount = float(m.group(1)) * _SUFFIX_MULTIPLIER[m.group(2).lower()]
        if best is None or amount > best:
            best = amount
    for m in _DOLLAR_WORD_RE.finditer(text):
        amount = float(m.group(1)) * _WORD_MULTIPLIER[m.group(2).lower()]
        if best is None or amount > best:
            best = amount
    return best


def funding_signal(text: str) -> float:
    """Multiplier scaled to the size of the dollar amount in ``text``.

    Ranges (base 1.0): ``>=$10B`` -> 1.8, ``>=$1B`` -> 1.5, ``>=$100M`` -> 1.3,
    ``>=$10M`` -> 1.15, smaller amounts -> 1.05, no dollar figure -> 1.0.
    """
    amount = _parse_max_dollar_amount(text)
    if amount is None:
        return 1.0
    if amount >= 10e9:
        return 1.8
    if amount >= 1e9:
        return 1.5
    if amount >= 100e6:
        return 1.3
    if amount >= 10e6:
        return 1.15
    return 1.05


def models_signal(text: str) -> float:
    """Boost model-release headlines. Release verb: +0.2, model-family name: +0.2."""
    bonus = 1.0
    if _MODEL_RELEASE_VERB_RE.search(text):
        bonus += 0.2
    if _MODEL_NAME_RE.search(text):
        bonus += 0.2
    return bonus


def lawsuits_signal(text: str) -> float:
    """Boost court/regulator headlines; add a small kicker for a fine amount."""
    bonus = 1.0
    if _LAWSUIT_HIGH_RE.search(text):
        bonus += 0.25
    amount = _parse_max_dollar_amount(text)
    if amount is not None and amount >= 1e6:
        bonus += 0.1
    return bonus


def research_signal(text: str) -> float:
    """Boost SOTA/benchmark/arXiv headlines."""
    bonus = 1.0
    if _RESEARCH_HIGH_RE.search(text):
        bonus += 0.2
    return bonus


def industry_signal(text: str) -> float:
    """Boost exec-hire / layoff / acquisition headlines."""
    bonus = 1.0
    if _INDUSTRY_HIGH_RE.search(text):
        bonus += 0.2
    return bonus


_CATEGORY_SIGNALS: dict[str, Callable[[str], float]] = {
    "funding": funding_signal,
    "models": models_signal,
    "lawsuits_policy": lawsuits_signal,
    "research": research_signal,
    "industry_moves": industry_signal,
}


def category_signal_score(category: str, text: str) -> float:
    """Return the category-specific multiplier for ``text`` (``>= 1.0``)."""
    fn = _CATEGORY_SIGNALS.get(category)
    if fn is None:
        return 1.0
    return fn(text or "")


# ---------------------------------------------------------------------------
# Ranking (pure)
# ---------------------------------------------------------------------------


def score_cluster(
    cluster: ClusterInput,
    *,
    now: datetime,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> RankedCluster:
    """Score one cluster into a :class:`RankedCluster`.

    Total score is the product of the four factors — a zero on any single
    factor (e.g. an item with no timestamp) zeroes the story out entirely,
    which is intentional: unrankable items should not float to the top.
    """
    authority = cluster_authority(cluster.sources, default=weights.default_authority)
    earliest = min((s.first_seen_at for s in cluster.sources), default=None)
    recency = recency_score(earliest, now=now, half_life_hours=weights.recency_half_life_hours)
    multiplicity = multiplicity_score(cluster.sources)
    signal = category_signal_score(cluster.category, cluster.canonical_headline)
    total = authority * recency * multiplicity * signal
    return RankedCluster(
        cluster_id=cluster.id,
        category=cluster.category,
        canonical_headline=cluster.canonical_headline,
        primary_url=cluster.primary_url,
        score=total,
        authority=authority,
        recency=recency,
        multiplicity=multiplicity,
        category_signal=signal,
    )


_TIE_BREAK_MAX_DT = datetime.max.replace(tzinfo=UTC)


def rank_clusters(
    clusters: Iterable[ClusterInput],
    *,
    now: datetime,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> dict[str, list[RankedCluster]]:
    """Score every cluster and return them bucketed and ordered per category.

    The returned mapping *always* has one key per entry in
    :data:`~signalweek.ingest.classify.CATEGORIES`, even when the bucket is
    empty. Clusters whose ``category`` is not one of the five known buckets
    are placed into :data:`~signalweek.ingest.classify.FALLBACK_CATEGORY`
    rather than silently dropped.

    Within each bucket entries are sorted by descending ``score``; ties
    break by earliest ``first_seen_at`` (older first) then by ``cluster_id``
    for a fully deterministic ordering.
    """
    materialised = list(clusters)

    earliest_by_cluster: dict[int, datetime | None] = {
        c.id: min((s.first_seen_at for s in c.sources), default=None) for c in materialised
    }

    buckets: dict[str, list[RankedCluster]] = {cat: [] for cat in CATEGORIES}
    for cluster in materialised:
        ranked = score_cluster(cluster, now=now, weights=weights)
        bucket = ranked.category if ranked.category in buckets else FALLBACK_CATEGORY
        buckets[bucket].append(ranked)

    for items in buckets.values():
        items.sort(
            key=lambda r: (
                -r.score,
                earliest_by_cluster.get(r.cluster_id) or _TIE_BREAK_MAX_DT,
                r.cluster_id,
            )
        )
    return buckets


# ---------------------------------------------------------------------------
# DB-facing helper
# ---------------------------------------------------------------------------


def rank_clusters_from_db(
    bind: Session | Connection,
    *,
    now: datetime,
    assignments: Mapping[int, int] | None = None,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> dict[str, list[RankedCluster]]:
    """Load clusters and their sources from the DB and rank them.

    ``assignments`` maps each ``raw_items.id`` to its ``clusters.id`` — the
    exact structure returned by
    :func:`signalweek.ingest.cluster.cluster_raw_items`. When omitted, cluster
    membership is inferred by matching each raw_item's ``canonical_url``
    against the canonical form of a cluster's ``primary_url``; this covers the
    common case but misses raw_items that joined a cluster via the fuzzy-title
    fallback. Prefer passing ``assignments`` explicitly when they're available.
    """
    connection = _as_connection(bind)

    cluster_rows = connection.execute(
        select(
            clusters_table.c.id,
            clusters_table.c.category,
            clusters_table.c.canonical_headline,
            clusters_table.c.primary_url,
        )
    ).all()

    clusters_by_id: dict[int, tuple[str, str, str]] = {
        int(r.id): (r.category, r.canonical_headline, r.primary_url) for r in cluster_rows
    }

    sources_by_cluster: dict[int, list[ClusterSource]] = defaultdict(list)

    if assignments is not None:
        raw_item_ids = list(assignments.keys())
        if raw_item_ids:
            rows = connection.execute(
                select(
                    raw_items_table.c.id,
                    sources_table.c.url,
                    raw_items_table.c.first_seen_at,
                )
                .select_from(
                    raw_items_table.join(
                        sources_table,
                        raw_items_table.c.source_id == sources_table.c.id,
                    )
                )
                .where(raw_items_table.c.id.in_(raw_item_ids))
            ).all()
            for row in rows:
                cluster_id = int(assignments[int(row.id)])
                if cluster_id not in clusters_by_id:
                    continue
                sources_by_cluster[cluster_id].append(
                    ClusterSource(
                        source_url=row.url,
                        first_seen_at=_ensure_aware(row.first_seen_at),
                    )
                )
    else:
        canon_to_cluster: dict[str, int] = {}
        for cid, (_, _, primary_url) in clusters_by_id.items():
            canon_to_cluster.setdefault(canonicalize_url(primary_url), cid)
        rows = connection.execute(
            select(
                raw_items_table.c.canonical_url,
                sources_table.c.url,
                raw_items_table.c.first_seen_at,
            ).select_from(
                raw_items_table.join(
                    sources_table,
                    raw_items_table.c.source_id == sources_table.c.id,
                )
            )
        ).all()
        for row in rows:
            cid = canon_to_cluster.get(row.canonical_url)
            if cid is None:
                continue
            sources_by_cluster[cid].append(
                ClusterSource(
                    source_url=row.url,
                    first_seen_at=_ensure_aware(row.first_seen_at),
                )
            )

    clusters = [
        ClusterInput(
            id=cid,
            category=category,
            canonical_headline=canonical_headline,
            primary_url=primary_url,
            sources=tuple(sources_by_cluster.get(cid, [])),
        )
        for cid, (category, canonical_headline, primary_url) in clusters_by_id.items()
    ]

    return rank_clusters(clusters, now=now, weights=weights)


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read — reattach UTC so subtraction works."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
