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
identical inputs produce byte-identical rows. Each item's "what happened"
(``items.summary``) is rule-based/extractive: the source body with the
repeated headline and feed boilerplate removed, cut at a sentence boundary
and hard-capped at :data:`WHAT_HAPPENED_MAX_WORDS` words (spec criterion 8)
— no LLM is invoked here. The byline (source publication name + source
publish date) is frozen onto the item at build time.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.classify import CATEGORIES, classify_clusters, is_research_only_url
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

# Spec criterion 8: the "what happened" body is at most 40 words.
WHAT_HAPPENED_MAX_WORDS = 40

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# arXiv listing bodies open with "arXiv:2609.21550v1 Announce Type: cross Abstract:".
_ARXIV_PREAMBLE_RE = re.compile(
    r"^arXiv:\S+\s+Announce Type:\s*\S+\s*(?:Abstract:\s*)?", re.IGNORECASE
)
# WordPress feeds append "The post <title> appeared first on <site>."
_WP_FOOTER_RE = re.compile(r"\s*The post .{0,300}? appeared first on .{0,120}$", re.IGNORECASE)
# Separators left behind once a leading headline is cut off.
_LEADING_SEPARATORS = " \t-–—:|."


@dataclass
class BuildResult:
    """Outcome of one :func:`build_issue` run.

    ``status`` is ``'held'`` (fewer than ``min_items`` items),
    ``'published'``, or ``'draft'`` when the caller asked to publish later via
    :func:`publish_issue`. ``items_per_category`` records how many items landed in
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
    publish: bool = True,
) -> BuildResult:
    """Assemble one weekly issue from the current DB state.

    ``now`` is the wall clock the build runs at — it drives the default
    ``week_of`` (the Monday of ``now``'s ISO week), the recency component of
    the ranker, and the ``published_at`` timestamp on a published issue.

    Raises :class:`IssueAlreadyExistsError` when an ``issues`` row for the
    same ``week_of`` already exists — building is a one-shot per week; a
    re-run should delete the previous row first.

    With ``publish=False`` an issue that clears ``min_items`` is left as
    ``'draft'`` so the caller can verify links before flipping it to
    ``'published'`` with :func:`publish_issue` (spec: build → verify → publish).
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
    anchors = _load_anchors(connection, {url for _, _, url in cluster_rows.values()})

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
    if total < min_items:
        status = "held"
    else:
        status = "published" if publish else "draft"

    issue_id = _insert_draft_issue(connection, week_of=week_of)
    _insert_items(
        connection,
        issue_id=issue_id,
        picked=picked,
        cluster_rows=cluster_rows,
        sources_by_cluster=sources_by_cluster,
        anchors=anchors,
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


def publish_issue(
    bind: Session | Connection,
    *,
    issue_id: int,
    now: datetime,
    min_items: int = DEFAULT_MIN_ITEMS,
) -> tuple[str, int]:
    """Flip a verified draft to ``'published'``, or ``'held'`` if too thin.

    Items are recounted here because verify may have dropped dead links since
    the build. Returns ``(status, item_count)``.
    """
    connection = _as_connection(bind)
    count = len(
        connection.execute(select(items_table.c.id).where(items_table.c.issue_id == issue_id)).all()
    )
    status = "published" if count >= min_items else "held"
    _finalise_status(connection, issue_id=issue_id, status=status, now=_ensure_aware(now))
    return status, count


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

    Membership is ``raw_items.cluster_id``, which the clustering pass in
    :mod:`signalweek.ingest.cluster` sets. A raw_item that has not been
    clustered yet falls back to matching its ``canonical_url`` against the
    canonical form of each cluster's ``primary_url``.
    """
    canon_by_cluster = {
        canonicalize_url(row.primary_url): int(row.id)
        for row in connection.execute(
            select(clusters_table.c.id, clusters_table.c.primary_url)
        ).all()
    }
    if not canon_by_cluster:
        return set()

    found: set[int] = set()
    for row in connection.execute(
        select(raw_items_table.c.canonical_url, raw_items_table.c.cluster_id).where(
            raw_items_table.c.first_seen_at >= cutoff
        )
    ).all():
        cid = _member_cluster(row.cluster_id, row.canonical_url, canon_by_cluster)
        if cid is not None:
            found.add(cid)
    return found


def _member_cluster(
    cluster_id: int | None, canonical_url: str, canon_to_cluster: dict[str, int]
) -> int | None:
    """Cluster a raw_item belongs to: its stored ``cluster_id``, else the
    cluster whose primary URL shares its canonical URL."""
    if cluster_id is not None:
        return int(cluster_id)
    return canon_to_cluster.get(canonical_url)


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
    # Canonical-URL fallback for raw_items that are not clustered yet.
    canon_to_cluster: dict[str, int] = {}
    for row in connection.execute(select(clusters_table.c.id, clusters_table.c.primary_url)).all():
        canon_to_cluster.setdefault(canonicalize_url(row.primary_url), int(row.id))

    grouped: dict[int, list[ClusterSource]] = {cid: [] for cid in cluster_ids}
    raw_rows = connection.execute(
        select(
            raw_items_table.c.canonical_url,
            raw_items_table.c.cluster_id,
            raw_items_table.c.first_seen_at,
            sources_table.c.url,
        ).select_from(
            raw_items_table.join(sources_table, raw_items_table.c.source_id == sources_table.c.id)
        )
    ).all()
    for row in raw_rows:
        cid = _member_cluster(row.cluster_id, row.canonical_url, canon_to_cluster)
        if cid not in grouped:
            continue
        grouped[cid].append(
            ClusterSource(
                source_url=row.url,
                first_seen_at=_ensure_aware(row.first_seen_at),
            )
        )
    return grouped


@dataclass(frozen=True)
class _Anchor:
    """The raw_item a cluster is named after, plus its source's byline."""

    body: str | None
    source_name: str | None
    published_at: datetime


def _load_anchors(connection: Connection, primary_urls: set[str]) -> dict[str, _Anchor]:
    """Fetch the anchor raw_item for each primary URL.

    The anchor is the raw_item whose ``url`` matches ``clusters.primary_url``
    exactly — that is how the clustering pass names it. If multiple raw_items
    share that URL (rare but possible when several sources publish the exact
    same link), the earliest by ``first_seen_at`` wins. The publish date is
    the feed entry's own stamp, or ``first_seen_at`` for undated entries.
    """
    if not primary_urls:
        return {}
    rows = connection.execute(
        select(
            raw_items_table.c.url,
            raw_items_table.c.body,
            raw_items_table.c.published_at,
            raw_items_table.c.first_seen_at,
            sources_table.c.name,
        )
        .select_from(
            raw_items_table.join(sources_table, raw_items_table.c.source_id == sources_table.c.id)
        )
        .where(raw_items_table.c.url.in_(primary_urls))
        .order_by(raw_items_table.c.first_seen_at.asc(), raw_items_table.c.id.asc())
    ).all()
    anchors: dict[str, _Anchor] = {}
    for row in rows:
        if row.url in anchors:
            continue
        anchors[row.url] = _Anchor(
            body=row.body,
            source_name=row.name,
            published_at=_ensure_aware(row.published_at or row.first_seen_at),
        )
    return anchors


def refresh_item_render_fields(bind: Session | Connection, *, issue_id: int | None = None) -> int:
    """Recompute the what-happened body and byline on already-built items.

    Used once to bring issues built before the render contract existed up to
    it; safe to re-run. Items whose anchor raw_item is gone keep their
    stored summary, cleaned and capped the same way. Returns the row count.
    """
    connection = _as_connection(bind)
    stmt = select(
        items_table.c.id,
        items_table.c.headline,
        items_table.c.summary,
        items_table.c.primary_url,
        items_table.c.source_name,
        items_table.c.source_published_at,
    )
    if issue_id is not None:
        stmt = stmt.where(items_table.c.issue_id == issue_id)
    rows = connection.execute(stmt).all()
    anchors = _load_anchors(connection, {row.primary_url for row in rows})
    for row in rows:
        anchor = anchors.get(row.primary_url)
        body = anchor.body if anchor is not None and anchor.body else row.summary
        headline = _clean_headline(row.headline)
        connection.execute(
            items_table.update()
            .where(items_table.c.id == row.id)
            .values(
                headline=headline,
                summary=build_what_happened(headline, body),
                source_name=_source_name(anchor, row.primary_url),
                source_published_at=(
                    anchor.published_at if anchor is not None else row.source_published_at
                ),
            )
        )
    return len(rows)


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
    anchors: dict[str, _Anchor],
) -> None:
    for position, ranked in enumerate(picked, start=1):
        cid = ranked.cluster_id
        headline = _clean_headline(ranked.canonical_headline)
        primary_url = ranked.primary_url
        anchor = anchors.get(primary_url)
        summary = build_what_happened(headline, anchor.body if anchor is not None else None)
        extras = _extra_source_urls(sources_by_cluster.get(cid, ()), primary_url)
        if ranked.category != "research":
            # arXiv links belong in Research only, citations included.
            extras = [url for url in extras if not is_research_only_url(url)]
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
                source_name=_source_name(anchor, primary_url),
                source_published_at=anchor.published_at if anchor is not None else None,
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


def build_what_happened(headline: str, body: str | None) -> str:
    """Return the item's "what happened" text: at most 40 words, no headline.

    HTML, the repeated headline and feed boilerplate (arXiv preambles,
    WordPress footers) are removed; then whole sentences are kept while they
    fit :data:`WHAT_HAPPENED_MAX_WORDS`. A single over-long first sentence is
    cut to the word budget and ends in "…". Returns ``""`` when nothing but
    the headline is left — the page then shows no body rather than a repeat.
    """
    if not body:
        return ""
    text = _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", body))).strip()
    headline_clean = _clean_headline(headline)
    previous = None
    while text != previous:
        previous = text
        text = _ARXIV_PREAMBLE_RE.sub("", text)
        if headline_clean and text.lower().startswith(headline_clean.lower()):
            text = _drop_headline(text, len(headline_clean))
        text = _WP_FOOTER_RE.sub("", text).strip()
    return _cap_words(text, WHAT_HAPPENED_MAX_WORDS)


def _clean_headline(headline: str) -> str:
    """Decode feed HTML entities ("Nvidia&#8217;s") and collapse whitespace;
    the template escapes on output, so stored text must be plain."""
    return _WHITESPACE_RE.sub(" ", html.unescape(headline)).strip()


def _drop_headline(text: str, headline_len: int) -> str:
    """Remove a leading headline from ``text``.

    "Headline — body" / "Headline: body" lose the headline and separator.
    When the headline instead opens a longer sentence ("Headline, a new …")
    that whole sentence goes if more follow; otherwise its tail is kept,
    capitalised, so the body never starts mid-sentence in lower case.
    """
    rest = text[headline_len:]
    if not rest or rest[0] in _LEADING_SEPARATORS:
        return rest.lstrip(_LEADING_SEPARATORS)
    sentences = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)
    if len(sentences) == 2:
        return sentences[1]
    rest = rest.lstrip(" ,;")
    return rest[:1].upper() + rest[1:]


def _cap_words(text: str, limit: int) -> str:
    kept: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        words = sentence.split()
        if len(kept) + len(words) <= limit:
            kept.extend(words)
            continue
        if not kept:
            return " ".join(words[:limit]).rstrip(",;:-–—") + "…"
        break
    return " ".join(kept)


def _source_name(anchor: _Anchor | None, primary_url: str) -> str:
    """Registry name of the primary source, else the article's host."""
    if anchor is not None and anchor.source_name:
        return anchor.source_name
    return _host(primary_url)


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
