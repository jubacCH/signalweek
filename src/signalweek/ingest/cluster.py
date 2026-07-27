"""Deduplicate raw_items into clusters.

Each ``raw_items`` row is assigned to a cluster using two match rules, in
order:

1. **Exact canonical-URL match** — the incoming item's canonical URL matches
   the canonical form of a cluster's ``primary_url`` (or the canonical URL of
   any raw_item already assigned to that cluster in the current run).
2. **Domain + fuzzy title match** — the incoming item shares a hostname with
   a cluster and its normalized title is similar enough to the cluster's
   ``canonical_headline`` under a token-ratio threshold.

If neither rule fires, a new cluster row is inserted.

Raw items are processed in ``first_seen_at`` order (oldest first). The first
raw_item to land in a cluster during a run anchors that cluster's
``primary_url`` and ``canonical_headline`` — this guarantees the earliest
URL/headline seen is what ends up on the cluster row, even if a later
run picks up a raw_item that predates the existing anchor.

There is no per-user logic here — the curated digest has global sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest.canonical import canonicalize_url
from signalweek.sources import clusters_table, raw_items_table, sources_table

DEFAULT_CATEGORY = "industry_moves"

# Minimum ``SequenceMatcher`` ratio between two normalized headlines for a
# domain-scoped fuzzy match. Chosen to keep near-identical headlines together
# ("OpenAI unveils GPT-5" vs "OpenAI unveils GPT-5 today") while rejecting
# same-domain articles with unrelated titles.
FUZZY_TITLE_THRESHOLD = 0.82


@dataclass
class ClusterRunResult:
    """Outcome of one :func:`cluster_raw_items` run.

    ``assignments`` maps each ``raw_items.id`` visited during the run to the
    ``clusters.id`` it landed in. ``created`` counts new cluster rows;
    ``matched`` counts raw_items that reused an existing cluster (whether
    from this run or a previous one). ``anchor_updates`` counts clusters
    whose ``primary_url``/``canonical_headline`` were rewritten because a
    newly-processed raw_item was earlier than the previous anchor.
    """

    assignments: dict[int, int] = field(default_factory=dict)
    created: int = 0
    matched: int = 0
    anchor_updates: int = 0

    @property
    def total(self) -> int:
        return len(self.assignments)


@dataclass
class _ClusterState:
    """In-memory view of one cluster row, kept in sync with the DB."""

    id: int
    primary_url: str
    canonical_headline: str
    category: str
    # True once a raw_item from the current run has anchored this cluster
    # (i.e. set its primary_url/canonical_headline). Later raw_items in the
    # same run — which are necessarily equal or later in first_seen_at — do
    # not overwrite it.
    anchored_this_run: bool = False


def cluster_raw_items(bind: Session | Connection) -> ClusterRunResult:
    """Group every ``raw_items`` row into a ``clusters`` row.

    Idempotent: calling it repeatedly produces the same clusters (given the
    same raw_items). New raw_items processed on a later run either match an
    existing cluster or spawn a new one; if a match is earlier in
    ``first_seen_at`` than the current anchor, the cluster's ``primary_url``
    and ``canonical_headline`` are rewritten to the earlier item's values.
    """
    connection = _as_connection(bind)

    clusters = _load_existing_clusters(connection)
    # ``canonical URL -> cluster`` index used by the exact-match rule. Seeded
    # from existing cluster primary URLs and extended as raw_items get
    # assigned during the run.
    canon_index: dict[str, _ClusterState] = {
        canonicalize_url(c.primary_url): c for c in clusters if c.primary_url
    }

    result = ClusterRunResult()

    rows = connection.execute(
        select(
            raw_items_table.c.id,
            raw_items_table.c.url,
            raw_items_table.c.canonical_url,
            raw_items_table.c.title,
            raw_items_table.c.first_seen_at,
            sources_table.c.category_hint,
        )
        .select_from(
            raw_items_table.join(sources_table, raw_items_table.c.source_id == sources_table.c.id)
        )
        .order_by(
            raw_items_table.c.first_seen_at.asc(),
            raw_items_table.c.id.asc(),
        )
    ).all()

    for row in rows:
        cluster = canon_index.get(row.canonical_url)
        if cluster is None:
            cluster = _find_fuzzy_match(row.url, row.title, clusters)

        if cluster is None:
            cluster = _create_cluster(
                connection,
                primary_url=row.url,
                canonical_headline=row.title,
                category=row.category_hint or DEFAULT_CATEGORY,
            )
            clusters.append(cluster)
            canon_index[row.canonical_url] = cluster
            cluster.anchored_this_run = True
            result.created += 1
            result.assignments[row.id] = cluster.id
            continue

        # Existing cluster: record the assignment and, if this is the first
        # raw_item this run to land here, rewrite the anchor.
        result.assignments[row.id] = cluster.id
        result.matched += 1
        canon_index.setdefault(row.canonical_url, cluster)

        if cluster.anchored_this_run:
            continue
        cluster.anchored_this_run = True

        if cluster.primary_url == row.url and cluster.canonical_headline == row.title:
            continue

        old_canon = canonicalize_url(cluster.primary_url)
        connection.execute(
            clusters_table.update()
            .where(clusters_table.c.id == cluster.id)
            .values(primary_url=row.url, canonical_headline=row.title)
        )
        cluster.primary_url = row.url
        cluster.canonical_headline = row.title
        # Keep the canonical-URL index pointing at the current anchor URL,
        # but do not drop the old key — it may still be needed to match
        # sibling raw_items that share the previous anchor's canonical URL.
        canon_index[row.canonical_url] = cluster
        canon_index.setdefault(old_canon, cluster)
        result.anchor_updates += 1

    return result


def _load_existing_clusters(connection: Connection) -> list[_ClusterState]:
    stmt = select(
        clusters_table.c.id,
        clusters_table.c.primary_url,
        clusters_table.c.canonical_headline,
        clusters_table.c.category,
    )
    return [
        _ClusterState(
            id=int(r.id),
            primary_url=r.primary_url,
            canonical_headline=r.canonical_headline,
            category=r.category,
        )
        for r in connection.execute(stmt).all()
    ]


def _create_cluster(
    connection: Connection,
    *,
    primary_url: str,
    canonical_headline: str,
    category: str,
) -> _ClusterState:
    inserted = connection.execute(
        clusters_table.insert()
        .values(
            primary_url=primary_url,
            canonical_headline=canonical_headline,
            category=category,
        )
        .returning(clusters_table.c.id)
    )
    return _ClusterState(
        id=int(inserted.scalar_one()),
        primary_url=primary_url,
        canonical_headline=canonical_headline,
        category=category,
    )


def _find_fuzzy_match(url: str, title: str, clusters: list[_ClusterState]) -> _ClusterState | None:
    domain = _domain(url)
    normalized_new = _normalize_title(title)
    if not domain or not normalized_new:
        return None

    best_ratio = FUZZY_TITLE_THRESHOLD
    best: _ClusterState | None = None
    for cluster in clusters:
        if _domain(cluster.primary_url) != domain:
            continue
        normalized_existing = _normalize_title(cluster.canonical_headline)
        if not normalized_existing:
            continue
        ratio = SequenceMatcher(None, normalized_new, normalized_existing).ratio()
        if ratio >= best_ratio:
            best_ratio = ratio
            best = cluster
    return best


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    lowered = title.lower()
    without_symbols = _NON_ALNUM_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", without_symbols).strip()


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
