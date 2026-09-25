"""Deduplicate raw_items into clusters.

Each unclustered ``raw_items`` row (``cluster_id IS NULL``) is assigned to a
cluster using two match rules, in order:

1. **Exact canonical-URL match**: the item's canonical URL matches the
   canonical form of a cluster's ``primary_url`` or the canonical URL of any
   raw_item already in that cluster.
2. **Headline embedding match**: the cosine similarity between the item's
   headline embedding and the headline embedding of some already-clustered
   raw_item is at least :data:`SIMILARITY_THRESHOLD`. Only raw_items first
   seen within :data:`DEDUP_WINDOW` of the item are compared, and the most
   similar one wins. Embeddings come from a local model
   (:mod:`signalweek.ingest.embed`).

If neither rule fires, a new cluster row is inserted.

The pass is incremental. Each raw_item is embedded and clustered once, and
both the embedding (``raw_items.title_embedding``) and the membership
(``raw_items.cluster_id``) are stored, so an hourly tick only does work for
new items. The first run after migration 0009 backfills every existing
raw_item.

Pending raw_items are processed in ``first_seen_at`` order (oldest first). A
cluster's ``primary_url`` and ``canonical_headline`` always come from its
earliest member. If a newly clustered raw_item predates the current anchor,
the cluster row is rewritten to the new item's values.

There is no per-user logic here. The curated digest has global sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import bindparam, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest import embed as _embed
from signalweek.ingest.canonical import canonicalize_url
from signalweek.sources import clusters_table, raw_items_table, sources_table

DEFAULT_CATEGORY = "industry_moves"

# Spec criterion 10: two items are one story when their headlines have cosine
# similarity >= 0.85 on a sentence-embedding model.
SIMILARITY_THRESHOLD = 0.85

# How far apart (by ``first_seen_at``) two raw_items may be and still merge on
# headline similarity. This bounds the per-tick comparison set. Older repeats
# are caught by the builder's cross-issue URL guard.
DEDUP_WINDOW = timedelta(days=30)


@dataclass
class ClusterRunResult:
    """Outcome of one :func:`cluster_raw_items` run.

    ``assignments`` maps each ``raw_items.id`` clustered during the run to the
    ``clusters.id`` it landed in. ``created`` counts new cluster rows.
    ``matched`` counts raw_items that joined an existing cluster, whether it
    came from this run or an earlier one. ``semantic_matches`` is the subset of
    ``matched`` that joined on headline similarity rather than URL.
    ``anchor_updates`` counts clusters whose ``primary_url``/``canonical_headline``
    were rewritten because a newly clustered raw_item predated the previous
    anchor. ``embedded`` counts headlines that were embedded during this run.
    """

    assignments: dict[int, int] = field(default_factory=dict)
    created: int = 0
    matched: int = 0
    semantic_matches: int = 0
    anchor_updates: int = 0
    embedded: int = 0

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
    # ``first_seen_at`` of the earliest member, or None if no member is known.
    anchor_seen_at: datetime | None = None


class _Pool:
    """Headline embeddings of clustered raw_items, used by the similarity rule."""

    def __init__(self, capacity: int) -> None:
        self.vectors = np.empty((capacity, _embed.MODEL_DIM), dtype=np.float32)
        self.seen = np.empty(capacity, dtype=np.float64)
        self.cluster_ids = np.empty(capacity, dtype=np.int64)
        self.size = 0

    def add(self, vector: np.ndarray, seen_at: datetime, cluster_id: int) -> None:
        self.vectors[self.size] = vector
        self.seen[self.size] = _epoch(seen_at)
        self.cluster_ids[self.size] = cluster_id
        self.size += 1

    def best_match(self, vector: np.ndarray, seen_at: datetime) -> int | None:
        """Return the cluster id of the most similar in-window headline, if any
        reaches :data:`SIMILARITY_THRESHOLD`."""
        if self.size == 0:
            return None
        sims = self.vectors[: self.size] @ vector
        too_far = np.abs(self.seen[: self.size] - _epoch(seen_at)) > DEDUP_WINDOW.total_seconds()
        sims[too_far] = -1.0
        best = int(np.argmax(sims))
        if sims[best] >= SIMILARITY_THRESHOLD:
            return int(self.cluster_ids[best])
        return None


def cluster_raw_items(
    bind: Session | Connection, *, embedder: _embed.Embedder | None = None
) -> ClusterRunResult:
    """Assign every unclustered ``raw_items`` row to a ``clusters`` row.

    Idempotent. Once a raw_item has a ``cluster_id``, later runs skip it, so
    a second run with no new raw_items does nothing. ``embedder`` defaults to
    the baked-in local model and is only loaded if there are headlines to
    embed.
    """
    connection = _as_connection(bind)
    result = ClusterRunResult()

    pending = connection.execute(
        select(
            raw_items_table.c.id,
            raw_items_table.c.url,
            raw_items_table.c.canonical_url,
            raw_items_table.c.title,
            raw_items_table.c.first_seen_at,
            raw_items_table.c.title_embedding,
            sources_table.c.category_hint,
        )
        .select_from(
            raw_items_table.join(sources_table, raw_items_table.c.source_id == sources_table.c.id)
        )
        .where(raw_items_table.c.cluster_id.is_(None))
        .order_by(
            raw_items_table.c.first_seen_at.asc(),
            raw_items_table.c.id.asc(),
        )
    ).all()
    if not pending:
        return result

    vectors = _embed_pending(connection, pending, embedder, result)

    clusters = _load_existing_clusters(connection)
    # ``canonical URL -> cluster`` index for the exact-match rule: every
    # cluster's primary URL plus every clustered raw_item's canonical URL.
    canon_index: dict[str, _ClusterState] = {
        canonicalize_url(c.primary_url): c for c in clusters.values() if c.primary_url
    }
    for row in connection.execute(
        select(raw_items_table.c.canonical_url, raw_items_table.c.cluster_id).where(
            raw_items_table.c.cluster_id.is_not(None)
        )
    ).all():
        cluster = clusters.get(int(row.cluster_id))
        if cluster is not None:
            canon_index.setdefault(row.canonical_url, cluster)

    pool = _load_pool(
        connection,
        start=pending[0].first_seen_at - DEDUP_WINDOW,
        end=pending[-1].first_seen_at + DEDUP_WINDOW,
        extra_capacity=len(pending),
    )

    for row in pending:
        vector = vectors[int(row.id)]
        cluster = canon_index.get(row.canonical_url)
        if cluster is None and _has_words(row.title):
            match_id = pool.best_match(vector, row.first_seen_at)
            if match_id is not None:
                cluster = clusters[match_id]
                result.semantic_matches += 1

        if cluster is None:
            cluster = _create_cluster(
                connection,
                primary_url=row.url,
                canonical_headline=row.title,
                category=row.category_hint or DEFAULT_CATEGORY,
            )
            cluster.anchor_seen_at = row.first_seen_at
            clusters[cluster.id] = cluster
            result.created += 1
        else:
            result.matched += 1
            if cluster.anchor_seen_at is None or row.first_seen_at < cluster.anchor_seen_at:
                cluster.anchor_seen_at = row.first_seen_at
                if _rewrite_anchor(connection, cluster, url=row.url, headline=row.title):
                    canon_index[canonicalize_url(row.url)] = cluster
                    result.anchor_updates += 1

        canon_index.setdefault(row.canonical_url, cluster)
        result.assignments[int(row.id)] = cluster.id
        if _has_words(row.title):
            pool.add(vector, row.first_seen_at, cluster.id)

    connection.execute(
        raw_items_table.update()
        .where(raw_items_table.c.id == bindparam("raw_id"))
        .values(cluster_id=bindparam("new_cluster_id")),
        [
            {"raw_id": raw_id, "new_cluster_id": cluster_id}
            for raw_id, cluster_id in result.assignments.items()
        ],
    )
    return result


def _embed_pending(
    connection: Connection,
    pending: list,
    embedder: _embed.Embedder | None,
    result: ClusterRunResult,
) -> dict[int, np.ndarray]:
    """Return ``raw_item id -> headline vector``, embedding and storing only
    the headlines that have no stored embedding yet."""
    vectors = {
        int(r.id): _embed.from_blob(r.title_embedding)
        for r in pending
        if r.title_embedding is not None
    }
    missing = [r for r in pending if r.title_embedding is None]
    if not missing:
        return vectors

    embedder = embedder or _embed.get_default_embedder()
    fresh = embedder.embed([r.title for r in missing])
    connection.execute(
        raw_items_table.update()
        .where(raw_items_table.c.id == bindparam("raw_id"))
        .values(title_embedding=bindparam("blob")),
        [
            {"raw_id": int(r.id), "blob": _embed.to_blob(v)}
            for r, v in zip(missing, fresh, strict=True)
        ],
    )
    for r, v in zip(missing, fresh, strict=True):
        vectors[int(r.id)] = v
    result.embedded = len(missing)
    return vectors


def _load_existing_clusters(connection: Connection) -> dict[int, _ClusterState]:
    clusters = {
        int(r.id): _ClusterState(
            id=int(r.id),
            primary_url=r.primary_url,
            canonical_headline=r.canonical_headline,
            category=r.category,
        )
        for r in connection.execute(
            select(
                clusters_table.c.id,
                clusters_table.c.primary_url,
                clusters_table.c.canonical_headline,
                clusters_table.c.category,
            )
        ).all()
    }
    earliest = connection.execute(
        select(raw_items_table.c.cluster_id, func.min(raw_items_table.c.first_seen_at))
        .where(raw_items_table.c.cluster_id.is_not(None))
        .group_by(raw_items_table.c.cluster_id)
    ).all()
    for cluster_id, seen_at in earliest:
        cluster = clusters.get(int(cluster_id))
        if cluster is not None:
            cluster.anchor_seen_at = seen_at
    return clusters


def _load_pool(
    connection: Connection, *, start: datetime, end: datetime, extra_capacity: int
) -> _Pool:
    rows = connection.execute(
        select(
            raw_items_table.c.title,
            raw_items_table.c.first_seen_at,
            raw_items_table.c.cluster_id,
            raw_items_table.c.title_embedding,
        ).where(
            raw_items_table.c.cluster_id.is_not(None),
            raw_items_table.c.title_embedding.is_not(None),
            raw_items_table.c.first_seen_at >= start,
            raw_items_table.c.first_seen_at <= end,
        )
    ).all()
    pool = _Pool(len(rows) + extra_capacity)
    for r in rows:
        if _has_words(r.title):
            pool.add(_embed.from_blob(r.title_embedding), r.first_seen_at, int(r.cluster_id))
    return pool


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


def _rewrite_anchor(
    connection: Connection, cluster: _ClusterState, *, url: str, headline: str
) -> bool:
    if cluster.primary_url == url and cluster.canonical_headline == headline:
        return False
    connection.execute(
        clusters_table.update()
        .where(clusters_table.c.id == cluster.id)
        .values(primary_url=url, canonical_headline=headline)
    )
    cluster.primary_url = url
    cluster.canonical_headline = headline
    return True


_WORD_RE = re.compile(r"\w")


def _has_words(title: str) -> bool:
    """Headlines with no letters or digits carry no meaning to compare."""
    return bool(_WORD_RE.search(title or ""))


def _epoch(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
