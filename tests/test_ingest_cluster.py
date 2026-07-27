"""Tests for :mod:`signalweek.ingest.cluster`.

Each test builds a fresh SQLite database with the three Core tables the
clustering pass touches (``sources``, ``raw_items``, ``clusters``), seeds
raw_items directly, then asserts the state of the ``clusters`` table and
the returned :class:`ClusterRunResult`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest.cluster import cluster_raw_items
from signalweek.sources import (
    clusters_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)

BASE_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_source(
    conn: Connection,
    *,
    url: str,
    category_hint: str = "industry_moves",
    kind: str = "rss",
) -> int:
    result = conn.execute(
        sources_table.insert()
        .values(url=url, kind=kind, category_hint=category_hint, active=True)
        .returning(sources_table.c.id)
    )
    return int(result.scalar_one())


def _insert_raw_item(
    conn: Connection,
    *,
    source_id: int,
    url: str,
    canonical_url: str,
    title: str,
    first_seen_at: datetime,
) -> int:
    result = conn.execute(
        raw_items_table.insert()
        .values(
            source_id=source_id,
            url=url,
            canonical_url=canonical_url,
            title=title,
            body=None,
            fetched_at=first_seen_at,
            first_seen_at=first_seen_at,
        )
        .returning(raw_items_table.c.id)
    )
    return int(result.scalar_one())


def _all_clusters(conn: Connection) -> list[dict[str, object]]:
    stmt = select(clusters_table).order_by(clusters_table.c.id)
    return [dict(row._mapping) for row in conn.execute(stmt).all()]


# ---------------------------------------------------------------------------
# cluster_raw_items
# ---------------------------------------------------------------------------


def test_cluster_with_no_raw_items_creates_nothing(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)

    assert result.assignments == {}
    assert result.created == 0
    assert result.matched == 0
    with curated_engine.begin() as conn:
        assert _all_clusters(conn) == []


def test_single_raw_item_becomes_a_new_cluster(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(
            conn, url="https://blog.example.com/feed", category_hint="models"
        )
        raw_id = _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/a?utm_source=rss",
            canonical_url="https://blog.example.com/posts/a",
            title="OpenAI launches GPT-5",
            first_seen_at=BASE_TIME,
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert result.created == 1
    assert result.matched == 0
    assert result.anchor_updates == 0
    assert list(result.assignments.keys()) == [raw_id]
    assert len(clusters) == 1
    cluster = clusters[0]
    # The primary URL is the raw URL of the (only, hence earliest) raw_item.
    assert cluster["primary_url"] == "https://blog.example.com/posts/a?utm_source=rss"
    assert cluster["canonical_headline"] == "OpenAI launches GPT-5"
    assert cluster["category"] == "models"


def test_two_raw_items_sharing_canonical_url_collapse_into_one_cluster(
    curated_engine: Engine,
) -> None:
    """Different tracking params, same canonical URL — one cluster."""
    with curated_engine.begin() as conn:
        source_a = _insert_source(conn, url="https://a.example.com/feed")
        source_b = _insert_source(conn, url="https://b.example.com/feed")
        early = _insert_raw_item(
            conn,
            source_id=source_a,
            url="https://blog.example.com/posts/x?utm_source=a",
            canonical_url="https://blog.example.com/posts/x",
            title="Nvidia announces H200",
            first_seen_at=BASE_TIME,
        )
        later = _insert_raw_item(
            conn,
            source_id=source_b,
            url="https://blog.example.com/posts/x?utm_source=b",
            canonical_url="https://blog.example.com/posts/x",
            title="Nvidia announces H200 today",
            first_seen_at=BASE_TIME + timedelta(hours=2),
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert result.created == 1
    assert result.matched == 1
    assert len(clusters) == 1
    # Earliest raw_item anchors the cluster.
    assert result.assignments[early] == result.assignments[later]
    assert clusters[0]["primary_url"] == "https://blog.example.com/posts/x?utm_source=a"
    assert clusters[0]["canonical_headline"] == "Nvidia announces H200"


def test_domain_plus_fuzzy_title_match_collapses_near_duplicate_headlines(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(
            conn, url="https://news.example.com/feed", category_hint="funding"
        )
        early = _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://news.example.com/2026/07/anthropic-raises-round",
            canonical_url="https://news.example.com/2026/07/anthropic-raises-round",
            title="Anthropic raises Series F at $60B valuation",
            first_seen_at=BASE_TIME,
        )
        later = _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://news.example.com/2026/07/anthropic-round-detail",
            canonical_url="https://news.example.com/2026/07/anthropic-round-detail",
            title="Anthropic raises Series F at $60B valuation (update)",
            first_seen_at=BASE_TIME + timedelta(hours=1),
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert result.created == 1
    assert result.matched == 1
    assert result.assignments[early] == result.assignments[later]
    assert len(clusters) == 1
    assert clusters[0]["primary_url"] == ("https://news.example.com/2026/07/anthropic-raises-round")
    assert clusters[0]["category"] == "funding"


def test_same_title_but_different_domains_are_kept_apart(curated_engine: Engine) -> None:
    """Fuzzy matching is scoped to a single hostname — no cross-domain merges."""
    with curated_engine.begin() as conn:
        source_a = _insert_source(conn, url="https://a.example.com/feed")
        source_b = _insert_source(conn, url="https://b.example.com/feed")
        _insert_raw_item(
            conn,
            source_id=source_a,
            url="https://a.example.com/posts/1",
            canonical_url="https://a.example.com/posts/1",
            title="The state of open source models",
            first_seen_at=BASE_TIME,
        )
        _insert_raw_item(
            conn,
            source_id=source_b,
            url="https://b.example.com/posts/9",
            canonical_url="https://b.example.com/posts/9",
            title="The state of open source models",
            first_seen_at=BASE_TIME + timedelta(minutes=30),
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert result.created == 2
    assert result.matched == 0
    assert len(clusters) == 2
    assert {c["primary_url"] for c in clusters} == {
        "https://a.example.com/posts/1",
        "https://b.example.com/posts/9",
    }


def test_unrelated_headlines_on_same_domain_are_separate_clusters(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/a",
            canonical_url="https://blog.example.com/a",
            title="OpenAI unveils new model family",
            first_seen_at=BASE_TIME,
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/b",
            canonical_url="https://blog.example.com/b",
            title="EU passes updated AI act amendment",
            first_seen_at=BASE_TIME + timedelta(minutes=5),
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert result.created == 2
    assert result.matched == 0
    assert len(clusters) == 2


def test_processing_order_is_by_first_seen_at_ascending(curated_engine: Engine) -> None:
    """The earliest raw_item — regardless of insert order — anchors the cluster."""
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")
        # Insert the LATER raw_item first, then the EARLIER one.
        later = _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/y-late",
            canonical_url="https://blog.example.com/posts/y-late",
            title="Meta open-sources Llama 4 (mirror)",
            first_seen_at=BASE_TIME + timedelta(hours=3),
        )
        early = _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/y",
            canonical_url="https://blog.example.com/posts/y",
            title="Meta open-sources Llama 4",
            first_seen_at=BASE_TIME,
        )

    with curated_engine.begin() as conn:
        cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert len(clusters) == 1
    # Anchor is the earliest raw_item, not the first-inserted one.
    assert clusters[0]["primary_url"] == "https://blog.example.com/posts/y"
    assert clusters[0]["canonical_headline"] == "Meta open-sources Llama 4"
    # And both raw_items land in the same cluster.
    assert early != later  # sanity


def test_second_run_is_idempotent(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/p1",
            canonical_url="https://blog.example.com/posts/p1",
            title="A story",
            first_seen_at=BASE_TIME,
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/p2",
            canonical_url="https://blog.example.com/posts/p2",
            title="Another story",
            first_seen_at=BASE_TIME + timedelta(hours=1),
        )

    with curated_engine.begin() as conn:
        first = cluster_raw_items(conn)
    with curated_engine.begin() as conn:
        second = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert first.created == 2
    assert first.matched == 0
    # Second pass finds both existing clusters — nothing new, nothing rewritten.
    assert second.created == 0
    assert second.matched == 2
    assert second.anchor_updates == 0
    assert len(clusters) == 2


def test_new_raw_item_joins_existing_cluster_via_exact_canonical_url(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/z",
            canonical_url="https://blog.example.com/posts/z",
            title="Some announcement",
            first_seen_at=BASE_TIME,
        )
    with curated_engine.begin() as conn:
        cluster_raw_items(conn)

    with curated_engine.begin() as conn:
        # Another source republishes the same URL with tracking params.
        second_source = _insert_source(conn, url="https://other.example.com/feed")
        _insert_raw_item(
            conn,
            source_id=second_source,
            url="https://blog.example.com/posts/z?utm_source=elsewhere",
            canonical_url="https://blog.example.com/posts/z",
            title="Some announcement — mirrored",
            first_seen_at=BASE_TIME + timedelta(hours=4),
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert result.created == 0
    assert result.matched == 2
    assert len(clusters) == 1
    # The original anchor is preserved because the mirror is later.
    assert clusters[0]["primary_url"] == "https://blog.example.com/posts/z"
    assert clusters[0]["canonical_headline"] == "Some announcement"


def test_later_run_rewrites_anchor_when_an_earlier_raw_item_arrives(
    curated_engine: Engine,
) -> None:
    """If a fresh raw_item predates the current anchor, it becomes the new anchor."""
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://blog.example.com/feed")
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/late",
            canonical_url="https://blog.example.com/posts/late",
            title="Google DeepMind ships Gemini 3",
            first_seen_at=BASE_TIME + timedelta(hours=5),
        )
    with curated_engine.begin() as conn:
        cluster_raw_items(conn)
        clusters_before = _all_clusters(conn)
    assert clusters_before[0]["primary_url"] == "https://blog.example.com/posts/late"

    with curated_engine.begin() as conn:
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/early",
            canonical_url="https://blog.example.com/posts/early",
            title="Google DeepMind ships Gemini 3",
            first_seen_at=BASE_TIME,
        )

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters_after = _all_clusters(conn)

    assert result.created == 0
    assert result.matched == 2
    assert result.anchor_updates == 1
    assert len(clusters_after) == 1
    # Anchor moved to the earlier raw_item.
    assert clusters_after[0]["primary_url"] == "https://blog.example.com/posts/early"


def test_category_falls_back_when_source_has_no_hint(curated_engine: Engine) -> None:
    """A source with a NULL category_hint slots into the default bucket."""
    with curated_engine.begin() as conn:
        # Bypass the sources_table default so ``category_hint`` is stored as NULL.
        conn.execute(
            sources_table.insert().values(
                url="https://noisy.example.com/feed",
                kind="rss",
                category_hint=None,
                active=True,
            )
        )
        source_id = int(
            conn.execute(
                select(sources_table.c.id).where(
                    sources_table.c.url == "https://noisy.example.com/feed"
                )
            ).scalar_one()
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://noisy.example.com/posts/x",
            canonical_url="https://noisy.example.com/posts/x",
            title="Something happened",
            first_seen_at=BASE_TIME,
        )

    with curated_engine.begin() as conn:
        cluster_raw_items(conn)
        clusters = _all_clusters(conn)

    assert clusters[0]["category"] == "industry_moves"
