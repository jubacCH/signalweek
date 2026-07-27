"""Build the weekly issue: classify → rank → pick → materialize.

This module owns the "build" step of the pipeline. Upstream stages
(:mod:`signalweek.ingest.feeds`, :mod:`signalweek.ingest.cluster`) populate
``raw_items`` and ``clusters``; this module reads the last 7 days of
activity, classifies, ranks, dedups against recent issues, and writes an
``issues`` row with its attached ``items`` in the fixed 5-section order.

Cross-issue dedup: a candidate cluster whose ``primary_url`` (after URL
canonicalization) matches any ``items.primary_url`` from the last 12
*published* issues is dropped. The story has already run; we do not repeat
it. Held or draft issues do not consume the dedup budget — only published
ones do.

Hold guard: an issue built with fewer than ``min_items`` total items is
recorded as ``status='held'`` and never marked published. Callers can
inspect it and decide whether to fill the slot manually. A held issue's
items are still written to disk so an editor can see what the pipeline
came up with.

The build is deliberately deterministic given ``now`` and the DB state:
identical inputs produce byte-identical rows. All summaries are
rule-based/extractive (headline + short lede pulled from the source's
body) — no LLM is invoked here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.classify import CATEGORIES, classify_clusters
from signalweek.ranking import (
    DEFAULT_WEIGHTS,
    ClusterInput,
    ClusterSource,
    RankedCluster,
    RankingWeights,
    rank_clusters,
)
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    raw_items_table,
    sources_table,
)

# Number of stories included per category in a full issue.
DEFAULT_TOP_N_PER_CATEGORY = 5

# An issue with fewer than this many total items is held rather than published.
DEFAULT_MIN_ITEMS = 10

# How many days of ``raw_items`` activity feed a single weekly build.
DEFAULT_LOOKBACK_DAYS = 7

# How many recently-published issues participate in cross-issue URL dedup.
DEFAULT_DEDUP_WINDOW_ISSUES = 12

# Maximum characters retained from a raw_item body for the item summary.
_SUMMARY_MAX_CHARS = 350

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")


@dataclass
class BuildResult:
    """Outcome of one :func:`build_issue` run.

    ``status`` is either ``'held'`` (fewer than ``min_items`` items) or
    ``'published'``. ``items_per_category`` records how many items landed in
    each of the five fixed buckets; ``rejected_by_dedup`` counts candidate
    clusters dropped by the 12-week URL dedup guard.
    """

    issue_id: int
    week_of: date
    status: str
    total_items: int
    items_per_category: dict[str, int] = field(default_factory=dict)
    rejected_by_dedup: int = 0
    candidates_considered: int = 0


class IssueAlreadyExistsError(RuntimeError):
    """Raised when an ``issues`` row already exists for the requested week."""


def build_issue(
    bind: Session | Connection,
    *,
    now: datetime,
    week_of: date | None = None,
    top_n_per_category: int = DEFAULT_TOP_N_PER_CATEGORY,
    min_items: int = DEFAULT_MIN_ITEMS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dedup_window_issues: int = DEFAULT_DEDUP_WINDOW_ISSUES,
    weights: RankingWeights = DEFAULT_WEIGHTS,
) -> BuildResult:
    """Assemble one weekly issue from the current DB state.

    ``now`` is the wall clock the build runs at — it drives the default
    ``week_of`` (the Monday of ``now``'s ISO week), the recency component of
    the ranker, and the ``published_at`` timestamp on a published issue.

    Raises :class:`IssueAlreadyExistsError` when an ``issues`` row for the
    same ``week_of`` already exists — building is a one-shot per week; a
    re-run should delete the previous row first.
    """
    connection = _as_connection(bind)
    now = _ensure_aware(now)
    if week_of is None:
        week_of = _monday_of(now)

    _ensure_no_existing_issue(connection, week_of)

    # Refresh classifications on every cluster before ranking. The pipeline
    # normally runs the standalone classifier first, but re-running here keeps
    # the build self-contained and stays idempotent when categories drift.
    classify_clusters(connection)

    candidate_cluster_ids = _find_recent_cluster_ids(
        connection, cutoff=now - timedelta(days=lookback_days)
    )

    dedup_urls = _recent_published_primary_urls(connection, window=dedup_window_issues)

    cluster_rows = _load_clusters(connection, candidate_cluster_ids)
    sources_by_cluster = _load_cluster_sources(connection, candidate_cluster_ids)
    primary_bodies = _load_primary_bodies(connection, cluster_rows)

    rejected = 0
    inputs: list[ClusterInput] = []
    for cid, (category, headline, primary_url) in cluster_rows.items():
        if canonicalize_url(primary_url) in dedup_urls:
            rejected += 1
            continue
        inputs.append(
            ClusterInput(
                id=cid,
                category=category,
                canonical_headline=headline,
                primary_url=primary_url,
                sources=tuple(sources_by_cluster.get(cid, ())),
            )
        )

    ranked = rank_clusters(inputs, now=now, weights=weights)

    picked: list[RankedCluster] = []
    per_category_count: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for category in CATEGORIES:
        top = ranked[category][:top_n_per_category]
        per_category_count[category] = len(top)
        picked.extend(top)

    total = len(picked)
    status = "published" if total >= min_items else "held"

    issue_id = _insert_draft_issue(connection, week_of=week_of)
    _insert_items(
        connection,
        issue_id=issue_id,
        picked=picked,
        cluster_rows=cluster_rows,
        sources_by_cluster=sources_by_cluster,
        primary_bodies=primary_bodies,
    )
    _finalise_status(connection, issue_id=issue_id, status=status, now=now)

    return BuildResult(
        issue_id=issue_id,
        week_of=week_of,
        status=status,
        total_items=total,
        items_per_category=per_category_count,
        rejected_by_dedup=rejected,
        candidates_considered=len(cluster_rows),
    )


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------


def _ensure_no_existing_issue(connection: Connection, week_of: date) -> None:
    existing = connection.execute(
        select(issues_table.c.id).where(issues_table.c.week_of == week_of)
    ).first()
    if existing is not None:
        raise IssueAlreadyExistsError(
            f"issue for week_of={week_of.isoformat()} already exists (id={int(existing.id)})"
        )


def _find_recent_cluster_ids(connection: Connection, *, cutoff: datetime) -> set[int]:
    """Return every cluster with at least one raw_item first-seen after ``cutoff``.

    Membership is inferred by matching ``raw_items.canonical_url`` against the
    canonical form of each cluster's ``primary_url``. Clusters that formed via
    the fuzzy-title fallback in :mod:`signalweek.ingest.cluster` are not
    guaranteed to share a canonical URL with their raw_items, but this build
    stage does not have access to the run's ``assignments`` map — the mapping
    lives only in memory during clustering. Callers who need perfect fidelity
    should run the build immediately after clustering.
    """
    canon_by_cluster = {
        canonicalize_url(row.primary_url): int(row.id)
        for row in connection.execute(
            select(clusters_table.c.id, clusters_table.c.primary_url)
        ).all()
    }
    if not canon_by_cluster:
        return set()

    recent_canonicals = {
        row.canonical_url
        for row in connection.execute(
            select(raw_items_table.c.canonical_url).where(raw_items_table.c.first_seen_at >= cutoff)
        ).all()
    }
    return {cid for canon, cid in canon_by_cluster.items() if canon in recent_canonicals}


def _recent_published_primary_urls(connection: Connection, *, window: int) -> set[str]:
    """Return the set of canonicalized primary URLs from the last ``window``
    published issues, used as the cross-issue dedup guard."""
    if window <= 0:
        return set()
    recent_issue_ids = [
        int(row.id)
        for row in connection.execute(
            select(issues_table.c.id)
            .where(issues_table.c.status == "published")
            .order_by(
                issues_table.c.published_at.desc().nulls_last(),
                issues_table.c.id.desc(),
            )
            .limit(window)
        ).all()
    ]
    if not recent_issue_ids:
        return set()

    return {
        canonicalize_url(row.primary_url)
        for row in connection.execute(
            select(items_table.c.primary_url).where(items_table.c.issue_id.in_(recent_issue_ids))
        ).all()
    }


def _load_clusters(
    connection: Connection, cluster_ids: set[int]
) -> dict[int, tuple[str, str, str]]:
    if not cluster_ids:
        return {}
    rows = connection.execute(
        select(
            clusters_table.c.id,
            clusters_table.c.category,
            clusters_table.c.canonical_headline,
            clusters_table.c.primary_url,
        ).where(clusters_table.c.id.in_(cluster_ids))
    ).all()
    return {int(r.id): (r.category, r.canonical_headline, r.primary_url) for r in rows}


def _load_cluster_sources(
    connection: Connection, cluster_ids: set[int]
) -> dict[int, list[ClusterSource]]:
    """Return every raw_item feeding each candidate cluster, projected to
    :class:`ClusterSource` for ranking."""
    if not cluster_ids:
        return {}
    # Precompute the mapping from canonical_url → cluster_id so we can group
    # raw_items to clusters in a single scan.
    rows = connection.execute(
        select(clusters_table.c.id, clusters_table.c.primary_url).where(
            clusters_table.c.id.in_(cluster_ids)
        )
    ).all()
    canon_to_cluster: dict[str, int] = {}
    for row in rows:
        canon_to_cluster.setdefault(canonicalize_url(row.primary_url), int(row.id))

    grouped: dict[int, list[ClusterSource]] = {cid: [] for cid in cluster_ids}
    raw_rows = connection.execute(
        select(
            raw_items_table.c.canonical_url,
            raw_items_table.c.first_seen_at,
            sources_table.c.url,
        ).select_from(
            raw_items_table.join(sources_table, raw_items_table.c.source_id == sources_table.c.id)
        )
    ).all()
    for row in raw_rows:
        cid = canon_to_cluster.get(row.canonical_url)
        if cid is None:
            continue
        grouped.setdefault(cid, []).append(
            ClusterSource(
                source_url=row.url,
                first_seen_at=_ensure_aware(row.first_seen_at),
            )
        )
    return grouped


def _load_primary_bodies(
    connection: Connection, cluster_rows: dict[int, tuple[str, str, str]]
) -> dict[int, str | None]:
    """Fetch the ``body`` text of each cluster's anchor raw_item.

    The anchor is the raw_item whose ``url`` matches ``clusters.primary_url``
    exactly — that is how the clustering pass names it. If multiple raw_items
    share that URL (rare but possible when several sources publish the exact
    same link), the earliest by ``first_seen_at`` wins.
    """
    if not cluster_rows:
        return {}
    primary_urls = {url for _, _, url in cluster_rows.values()}
    rows = connection.execute(
        select(
            raw_items_table.c.url,
            raw_items_table.c.body,
            raw_items_table.c.first_seen_at,
        )
        .where(raw_items_table.c.url.in_(primary_urls))
        .order_by(raw_items_table.c.first_seen_at.asc(), raw_items_table.c.id.asc())
    ).all()
    body_by_url: dict[str, str | None] = {}
    for row in rows:
        body_by_url.setdefault(row.url, row.body)
    return {cid: body_by_url.get(url) for cid, (_, _, url) in cluster_rows.items()}


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------


def _insert_draft_issue(connection: Connection, *, week_of: date) -> int:
    result = connection.execute(
        issues_table.insert()
        .values(week_of=week_of, status="draft", published_at=None)
        .returning(issues_table.c.id)
    )
    return int(result.scalar_one())


def _insert_items(
    connection: Connection,
    *,
    issue_id: int,
    picked: list[RankedCluster],
    cluster_rows: dict[int, tuple[str, str, str]],
    sources_by_cluster: dict[int, list[ClusterSource]],
    primary_bodies: dict[int, str | None],
) -> None:
    for position, ranked in enumerate(picked, start=1):
        cid = ranked.cluster_id
        headline = ranked.canonical_headline
        primary_url = ranked.primary_url
        summary = _build_summary(headline, primary_bodies.get(cid))
        extras = _extra_source_urls(sources_by_cluster.get(cid, ()), primary_url)
        connection.execute(
            items_table.insert().values(
                issue_id=issue_id,
                cluster_id=cid,
                category=ranked.category,
                position=position,
                headline=headline,
                summary=summary,
                primary_url=primary_url,
                extra_source_urls=extras,
            )
        )


def _finalise_status(connection: Connection, *, issue_id: int, status: str, now: datetime) -> None:
    values: dict[str, object] = {"status": status}
    if status == "published":
        values["published_at"] = now
    connection.execute(
        issues_table.update().where(and_(issues_table.c.id == issue_id)).values(**values)
    )


# ---------------------------------------------------------------------------
# Rule-based summary + helpers
# ---------------------------------------------------------------------------


def _build_summary(headline: str, body: str | None) -> str:
    """Return ``headline`` optionally followed by a short extractive lede.

    The lede is the first sentence (or the leading chunk truncated at
    ~350 characters) of the raw_item body, with HTML tags and repeated
    whitespace stripped. When the body is empty or reduces to the headline,
    only the headline is returned.
    """
    headline_clean = headline.strip()
    if not body:
        return headline_clean
    lede = _extract_lede(body)
    if not lede or _WHITESPACE_RE.sub(" ", lede.lower()) == headline_clean.lower():
        return headline_clean
    return f"{headline_clean} — {lede}"


def _extract_lede(body: str) -> str:
    """Strip HTML/whitespace from ``body`` and truncate to a sentence."""
    text = _HTML_TAG_RE.sub(" ", body)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return ""

    match = _SENTENCE_END_RE.search(text, endpos=_SUMMARY_MAX_CHARS + 1)
    if match:
        end = match.end() - 1  # include the punctuation, drop the trailing space
        candidate = text[:end].rstrip()
        if candidate:
            return candidate

    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    # Truncate on the last whitespace within the budget so we don't chop a word.
    truncated = text[:_SUMMARY_MAX_CHARS].rstrip()
    space = truncated.rfind(" ")
    if space > 0:
        truncated = truncated[:space].rstrip()
    return f"{truncated}…"


def _extra_source_urls(
    sources: list[ClusterSource] | tuple[ClusterSource, ...], primary_url: str
) -> list[str]:
    """Return distinct source-registry URLs feeding a cluster, ordered by first
    appearance and excluding the source that owns ``primary_url``.

    The comparison is by registry URL, not article URL — a story mirrored on
    two feeds from the same outlet still counts as one 'extra source'."""
    if not sources:
        return []
    ordered = sorted(sources, key=lambda s: (s.first_seen_at, s.source_url))
    primary_host = _host(primary_url)
    seen: set[str] = set()
    result: list[str] = []
    for src in ordered:
        if not src.source_url or src.source_url in seen:
            continue
        seen.add(src.source_url)
        # Skip the source that produced the primary URL, identified by host.
        if primary_host and _host(src.source_url) == primary_host:
            continue
        result.append(src.source_url)
    return result


def _monday_of(dt: datetime) -> date:
    """Return the Monday of ``dt``'s ISO week (Monday=0)."""
    d = dt.date()
    return d - timedelta(days=d.weekday())


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read — reattach UTC so datetime math works."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
