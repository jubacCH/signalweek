"""Tests for :mod:`signalweek.ranking`.

Split into three parts:

* Component-score tests exercise the pure factor functions (authority,
  recency, multiplicity, per-category signals) in isolation.
* ``rank_clusters`` tests cover the pure ordering behaviour: bucketing,
  sorting, tie-breaking, always-present categories.
* ``rank_clusters_from_db`` tests build a fresh in-memory SQLite with the
  curated tables, seed sources/raw_items/clusters directly, and assert the
  DB helper stitches everything together correctly — both with and without
  an explicit ``assignments`` map.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest.classify import CATEGORIES, FALLBACK_CATEGORY
from signalweek.ranking import (
    DEFAULT_AUTHORITY,
    ClusterInput,
    ClusterSource,
    RankingWeights,
    authority_for_url,
    category_signal_score,
    cluster_authority,
    funding_signal,
    industry_signal,
    lawsuits_signal,
    models_signal,
    multiplicity_score,
    rank_clusters,
    rank_clusters_from_db,
    recency_score,
    research_signal,
    score_cluster,
)
from signalweek.sources import (
    clusters_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def test_authority_for_known_primary_source_is_top_of_scale() -> None:
    assert authority_for_url("https://openai.com/blog/rss.xml") == 1.0
    assert authority_for_url("https://ftc.gov/news/rss") == 1.0
    assert authority_for_url("https://arxiv.org/rss/cs.AI") == 0.95


def test_authority_for_www_prefix_and_subdomains_inherits_parent() -> None:
    # www. is stripped.
    assert authority_for_url("https://www.nytimes.com/rss") == 0.9
    # Subdomain falls back to parent domain in the table.
    assert authority_for_url("https://blog.openai.com/feed.xml") == 1.0


def test_authority_for_unknown_domain_uses_default() -> None:
    assert authority_for_url("https://random-blog.example.com/feed") == DEFAULT_AUTHORITY
    assert authority_for_url("https://x.example.org/rss", default=0.2) == 0.2


def test_authority_for_empty_or_malformed_url_uses_default() -> None:
    assert authority_for_url("") == DEFAULT_AUTHORITY
    assert authority_for_url("not-a-url") == DEFAULT_AUTHORITY


def test_cluster_authority_is_max_across_sources() -> None:
    sources = (
        ClusterSource(source_url="https://random.example.com/feed", first_seen_at=NOW),
        ClusterSource(source_url="https://openai.com/blog/rss.xml", first_seen_at=NOW),
        ClusterSource(source_url="https://techcrunch.com/feed", first_seen_at=NOW),
    )
    assert cluster_authority(sources) == 1.0


def test_cluster_authority_empty_sources_returns_default() -> None:
    assert cluster_authority(()) == DEFAULT_AUTHORITY
    assert cluster_authority((), default=0.1) == 0.1


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------


def test_recency_is_one_when_published_now() -> None:
    assert recency_score(NOW, now=NOW, half_life_hours=24) == 1.0


def test_recency_halves_at_one_half_life() -> None:
    one_hl_ago = NOW - timedelta(hours=24)
    assert recency_score(one_hl_ago, now=NOW, half_life_hours=24) == pytest.approx(0.5)


def test_recency_decays_exponentially_over_multiple_half_lives() -> None:
    two_hl_ago = NOW - timedelta(hours=48)
    assert recency_score(two_hl_ago, now=NOW, half_life_hours=24) == pytest.approx(0.25)


def test_recency_returns_zero_for_missing_timestamp() -> None:
    assert recency_score(None, now=NOW, half_life_hours=24) == 0.0


def test_recency_returns_zero_for_non_positive_half_life() -> None:
    assert recency_score(NOW, now=NOW, half_life_hours=0) == 0.0
    assert recency_score(NOW, now=NOW, half_life_hours=-5) == 0.0


def test_recency_clamps_future_timestamps_to_one() -> None:
    """Clock skew must not push a story above the 1.0 recency ceiling."""
    future = NOW + timedelta(hours=6)
    assert recency_score(future, now=NOW, half_life_hours=24) == 1.0


# ---------------------------------------------------------------------------
# Multiplicity
# ---------------------------------------------------------------------------


def test_multiplicity_of_single_source_is_one() -> None:
    sources = (ClusterSource(source_url="https://a.example.com/feed", first_seen_at=NOW),)
    assert multiplicity_score(sources) == 1.0


def test_multiplicity_of_two_distinct_sources_is_two() -> None:
    sources = (
        ClusterSource(source_url="https://a.example.com/feed", first_seen_at=NOW),
        ClusterSource(source_url="https://b.example.com/feed", first_seen_at=NOW),
    )
    assert multiplicity_score(sources) == pytest.approx(2.0)


def test_multiplicity_dedupes_identical_source_urls() -> None:
    """Two raw_items from the same registered source count as one outlet."""
    sources = (
        ClusterSource(source_url="https://a.example.com/feed", first_seen_at=NOW),
        ClusterSource(
            source_url="https://a.example.com/feed", first_seen_at=NOW + timedelta(hours=1)
        ),
    )
    assert multiplicity_score(sources) == 1.0


def test_multiplicity_grows_logarithmically() -> None:
    sources = tuple(
        ClusterSource(source_url=f"https://s{i}.example.com/feed", first_seen_at=NOW)
        for i in range(4)
    )
    # 1 + log2(4) = 3.
    assert multiplicity_score(sources) == pytest.approx(3.0)


def test_multiplicity_of_empty_sources_is_one() -> None:
    assert multiplicity_score(()) == 1.0


# ---------------------------------------------------------------------------
# Category signals
# ---------------------------------------------------------------------------


def test_funding_signal_scales_with_dollar_amount() -> None:
    assert funding_signal("Anthropic raises Series F at $60B valuation") == 1.8
    assert funding_signal("Startup raises $500M Series C") == 1.3
    assert funding_signal("Startup raises $2 billion in new round") == 1.5
    assert funding_signal("Startup raises $25M Series A") == 1.15
    assert funding_signal("Startup raises $500K in pre-seed") == 1.05


def test_funding_signal_no_dollar_amount_is_neutral() -> None:
    assert funding_signal("Some funding news without a number") == 1.0


def test_funding_signal_takes_the_largest_number_present() -> None:
    """When several dollar figures appear, the biggest one drives the boost."""
    text = "Company raised $5M in seed but is now closing a $2B round"
    assert funding_signal(text) == 1.5


def test_models_signal_boosts_release_verbs_and_model_names() -> None:
    assert models_signal("OpenAI unveils GPT-5") == pytest.approx(1.4)
    assert models_signal("OpenAI ships something new") == pytest.approx(1.2)
    assert models_signal("Notes on GPT-4 usage patterns") == pytest.approx(1.2)
    assert models_signal("General industry commentary") == 1.0


def test_lawsuits_signal_boosts_court_and_regulator_cues() -> None:
    assert lawsuits_signal("Court rules against major AI vendor") > 1.0
    assert lawsuits_signal("Executive order signed on AI safety") > 1.0
    # Fine amount adds a small kicker on top.
    fined = lawsuits_signal("Regulator fined company $50M in settlement")
    court_only = lawsuits_signal("Regulator settled with company")
    assert fined > court_only


def test_research_signal_boosts_sota_and_preprint_cues() -> None:
    assert research_signal("New preprint proposes novel benchmark") > 1.0
    assert research_signal("Weekly research digest") == 1.0


def test_industry_signal_boosts_exec_and_layoff_cues() -> None:
    assert industry_signal("Google hires new CEO for AI division") > 1.0
    assert industry_signal("Meta announces layoffs across AI org") > 1.0
    assert industry_signal("Company holds monthly all-hands") == 1.0


def test_category_signal_score_dispatches_by_category() -> None:
    assert category_signal_score("funding", "Raises $10B in new round") == 1.8
    assert category_signal_score("models", "OpenAI unveils GPT-5") == pytest.approx(1.4)
    assert category_signal_score("lawsuits_policy", "Executive order signed") > 1.0
    assert category_signal_score("research", "novel preprint on benchmarks") > 1.0
    assert category_signal_score("industry_moves", "new CEO announced") > 1.0
    # Unknown category is neutral.
    assert category_signal_score("uncategorized", "anything at all") == 1.0


# ---------------------------------------------------------------------------
# score_cluster
# ---------------------------------------------------------------------------


def _cluster(
    *,
    cluster_id: int = 1,
    category: str = "models",
    headline: str = "OpenAI unveils GPT-5",
    primary_url: str = "https://openai.com/blog/gpt-5",
    sources: tuple[ClusterSource, ...] | None = None,
) -> ClusterInput:
    return ClusterInput(
        id=cluster_id,
        category=category,
        canonical_headline=headline,
        primary_url=primary_url,
        sources=sources
        or (ClusterSource(source_url="https://openai.com/blog/rss.xml", first_seen_at=NOW),),
    )


def test_score_cluster_multiplies_the_four_factors() -> None:
    cluster = _cluster()
    ranked = score_cluster(cluster, now=NOW)
    expected = ranked.authority * ranked.recency * ranked.multiplicity * ranked.category_signal
    assert ranked.score == pytest.approx(expected)


def test_score_cluster_records_component_factors_for_inspection() -> None:
    cluster = _cluster()
    ranked = score_cluster(cluster, now=NOW)
    assert ranked.authority == 1.0
    assert ranked.recency == 1.0
    assert ranked.multiplicity == 1.0
    assert ranked.category_signal == pytest.approx(1.4)


def test_score_cluster_uses_earliest_first_seen_at_for_recency() -> None:
    """A cluster with an old first-seen raw_item scores lower than a fresh one."""
    old = _cluster(
        sources=(
            ClusterSource(
                source_url="https://openai.com/blog/rss.xml",
                first_seen_at=NOW - timedelta(hours=72),
            ),
        )
    )
    fresh = _cluster(
        cluster_id=2,
        sources=(
            ClusterSource(
                source_url="https://openai.com/blog/rss.xml",
                first_seen_at=NOW,
            ),
        ),
    )
    assert score_cluster(fresh, now=NOW).score > score_cluster(old, now=NOW).score


def test_score_cluster_uses_max_authority_across_sources() -> None:
    weak = _cluster(
        sources=(ClusterSource(source_url="https://random.example.com/feed", first_seen_at=NOW),)
    )
    mixed = _cluster(
        cluster_id=2,
        sources=(
            ClusterSource(source_url="https://random.example.com/feed", first_seen_at=NOW),
            ClusterSource(source_url="https://openai.com/blog/rss.xml", first_seen_at=NOW),
        ),
    )
    assert score_cluster(mixed, now=NOW).authority == 1.0
    assert score_cluster(weak, now=NOW).authority == DEFAULT_AUTHORITY


def test_score_cluster_recency_zero_when_no_timestamps() -> None:
    """A cluster with no raw_items ranks at zero — safe fallback, not a crash."""
    cluster = ClusterInput(
        id=99,
        category="models",
        canonical_headline="A story",
        primary_url="https://x.example.com/x",
        sources=(),
    )
    ranked = score_cluster(cluster, now=NOW)
    assert ranked.recency == 0.0
    assert ranked.score == 0.0


# ---------------------------------------------------------------------------
# rank_clusters (pure)
# ---------------------------------------------------------------------------


def test_rank_clusters_returns_all_five_categories_even_when_empty() -> None:
    buckets = rank_clusters([], now=NOW)
    assert set(buckets.keys()) == set(CATEGORIES)
    assert all(items == [] for items in buckets.values())


def test_rank_clusters_buckets_items_by_category() -> None:
    clusters = [
        _cluster(cluster_id=1, category="models", headline="OpenAI unveils GPT-5"),
        _cluster(
            cluster_id=2,
            category="funding",
            headline="Anthropic raises Series F at $60B valuation",
            primary_url="https://anthropic.com/news/round",
            sources=(
                ClusterSource(source_url="https://anthropic.com/news/rss.xml", first_seen_at=NOW),
            ),
        ),
    ]
    buckets = rank_clusters(clusters, now=NOW)
    assert [r.cluster_id for r in buckets["models"]] == [1]
    assert [r.cluster_id for r in buckets["funding"]] == [2]
    assert buckets["lawsuits_policy"] == []


def test_rank_clusters_orders_by_descending_score_within_a_category() -> None:
    hot = _cluster(
        cluster_id=1,
        headline="OpenAI unveils GPT-5",
        sources=(
            ClusterSource(source_url="https://openai.com/blog/rss.xml", first_seen_at=NOW),
            ClusterSource(source_url="https://nytimes.com/rss", first_seen_at=NOW),
        ),
    )
    cold = _cluster(
        cluster_id=2,
        headline="Random blog post about AI",
        primary_url="https://random.example.com/x",
        sources=(
            ClusterSource(
                source_url="https://random.example.com/feed",
                first_seen_at=NOW - timedelta(hours=120),
            ),
        ),
    )
    buckets = rank_clusters([cold, hot], now=NOW)
    ordered_ids = [r.cluster_id for r in buckets["models"]]
    assert ordered_ids == [1, 2]
    assert buckets["models"][0].score > buckets["models"][1].score


def test_rank_clusters_tie_break_prefers_earlier_first_seen_then_lower_id() -> None:
    """Two clusters with identical scores order by earliest first_seen, then id."""
    later_earlier_id = _cluster(
        cluster_id=1,
        headline="A story",
        sources=(
            ClusterSource(
                source_url="https://openai.com/blog/rss.xml",
                first_seen_at=NOW - timedelta(hours=1),
            ),
        ),
    )
    earlier_later_id = _cluster(
        cluster_id=2,
        headline="A story",
        sources=(
            ClusterSource(
                source_url="https://openai.com/blog/rss.xml",
                first_seen_at=NOW - timedelta(hours=5),
            ),
        ),
    )
    # Same category, similar structure — but recency differs so scores differ
    # too. Force a tie by using identical recency: put both at the same age.
    tie_a = _cluster(
        cluster_id=5,
        headline="A story",
        sources=(
            ClusterSource(
                source_url="https://openai.com/blog/rss.xml",
                first_seen_at=NOW - timedelta(hours=2),
            ),
        ),
    )
    tie_b = _cluster(
        cluster_id=3,
        headline="A story",
        sources=(
            ClusterSource(
                source_url="https://openai.com/blog/rss.xml",
                first_seen_at=NOW - timedelta(hours=2),
            ),
        ),
    )
    buckets = rank_clusters([later_earlier_id, earlier_later_id, tie_a, tie_b], now=NOW)
    ordered = [r.cluster_id for r in buckets["models"]]
    # Newest recency wins first.
    assert ordered[0] == 1
    # Then the pair tied at -2h, ordered by cluster_id ascending.
    assert ordered[1:3] == [3, 5]
    assert ordered[3] == 2


def test_rank_clusters_is_deterministic_on_repeated_calls() -> None:
    clusters = [_cluster(cluster_id=i, headline=f"Story {i}") for i in range(5)]
    first = rank_clusters(clusters, now=NOW)
    second = rank_clusters(clusters, now=NOW)
    for cat in CATEGORIES:
        assert [r.cluster_id for r in first[cat]] == [r.cluster_id for r in second[cat]]


def test_rank_clusters_places_unknown_category_into_fallback_bucket() -> None:
    orphan = ClusterInput(
        id=1,
        category="not-a-known-category",
        canonical_headline="Weird headline",
        primary_url="https://x.example.com/x",
        sources=(ClusterSource(source_url="https://openai.com/blog/rss.xml", first_seen_at=NOW),),
    )
    buckets = rank_clusters([orphan], now=NOW)
    assert [r.cluster_id for r in buckets[FALLBACK_CATEGORY]] == [1]


def test_rank_clusters_multiplicity_boost_can_overcome_lower_authority() -> None:
    """A weakly-sourced story picked up by many outlets beats a lone strong source."""
    lone_strong = _cluster(
        cluster_id=1,
        headline="Notes",
        sources=(ClusterSource(source_url="https://openai.com/blog/rss.xml", first_seen_at=NOW),),
    )
    many_weak = _cluster(
        cluster_id=2,
        headline="Notes",
        sources=tuple(
            ClusterSource(source_url=f"https://weak{i}.example.com/feed", first_seen_at=NOW)
            for i in range(8)
        ),
    )
    buckets = rank_clusters([lone_strong, many_weak], now=NOW)
    ordered = [r.cluster_id for r in buckets["models"]]
    assert ordered[0] == 2


# ---------------------------------------------------------------------------
# rank_clusters_from_db
# ---------------------------------------------------------------------------


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
    category_hint: str | None = "models",
) -> int:
    result = conn.execute(
        sources_table.insert()
        .values(url=url, kind="rss", category_hint=category_hint, active=True)
        .returning(sources_table.c.id)
    )
    return int(result.scalar_one())


def _insert_raw_item(
    conn: Connection,
    *,
    source_id: int,
    url: str,
    canonical_url: str | None = None,
    title: str = "T",
    first_seen_at: datetime = NOW,
) -> int:
    result = conn.execute(
        raw_items_table.insert()
        .values(
            source_id=source_id,
            url=url,
            canonical_url=canonical_url or url,
            title=title,
            body=None,
            fetched_at=first_seen_at,
            first_seen_at=first_seen_at,
        )
        .returning(raw_items_table.c.id)
    )
    return int(result.scalar_one())


def _insert_cluster(
    conn: Connection,
    *,
    primary_url: str,
    canonical_headline: str,
    category: str = "models",
) -> int:
    result = conn.execute(
        clusters_table.insert()
        .values(
            primary_url=primary_url,
            canonical_headline=canonical_headline,
            category=category,
        )
        .returning(clusters_table.c.id)
    )
    return int(result.scalar_one())


def test_rank_clusters_from_db_with_no_clusters_returns_empty_buckets(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        buckets = rank_clusters_from_db(conn, now=NOW)
    assert set(buckets.keys()) == set(CATEGORIES)
    assert all(items == [] for items in buckets.values())


def test_rank_clusters_from_db_uses_assignments_when_provided(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        s_openai = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        s_verge = _insert_source(
            conn, url="https://theverge.com/rss.xml", category_hint="industry_moves"
        )
        raw_openai = _insert_raw_item(
            conn,
            source_id=s_openai,
            url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
        )
        raw_verge = _insert_raw_item(
            conn,
            source_id=s_verge,
            url="https://theverge.com/2026/07/gpt5-writeup",
            title="Verge writeup",
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
        )
        assignments = {raw_openai: cluster_id, raw_verge: cluster_id}

    with curated_engine.begin() as conn:
        buckets = rank_clusters_from_db(conn, now=NOW, assignments=assignments)

    models_bucket = buckets["models"]
    assert len(models_bucket) == 1
    ranked = models_bucket[0]
    assert ranked.cluster_id == cluster_id
    # Two distinct source registries → multiplicity = 2.
    assert ranked.multiplicity == pytest.approx(2.0)
    # openai.com is 1.0, theverge.com is 0.75 → max is 1.0.
    assert ranked.authority == 1.0


def test_rank_clusters_from_db_falls_back_to_canonical_url_lookup(
    curated_engine: Engine,
) -> None:
    """Without assignments, membership is inferred by canonical-URL match."""
    with curated_engine.begin() as conn:
        s_openai = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        s_mirror = _insert_source(
            conn, url="https://mirror.example.com/rss", category_hint="models"
        )
        # Two raw_items on the same canonical URL — one from each source.
        _insert_raw_item(
            conn,
            source_id=s_openai,
            url="https://openai.com/blog/gpt-5?utm_source=rss",
            canonical_url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
        )
        _insert_raw_item(
            conn,
            source_id=s_mirror,
            url="https://openai.com/blog/gpt-5?utm_source=mirror",
            canonical_url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
            first_seen_at=NOW - timedelta(hours=2),
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
        )

    with curated_engine.begin() as conn:
        buckets = rank_clusters_from_db(conn, now=NOW)

    assert [r.cluster_id for r in buckets["models"]] == [cluster_id]
    ranked = buckets["models"][0]
    # Two distinct registry sources → multiplicity = 2.
    assert ranked.multiplicity == pytest.approx(2.0)
    # Recency uses the *earliest* raw_item — 2h old, not "now".
    assert ranked.recency < 1.0


def test_rank_clusters_from_db_orders_across_a_mixed_batch(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        s_openai = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        s_nyt = _insert_source(conn, url="https://nytimes.com/rss.xml")
        s_random = _insert_source(conn, url="https://random.example.com/feed")

        # Big, fresh, multi-source models story — expected #1 in models.
        r_hot = _insert_raw_item(
            conn,
            source_id=s_openai,
            url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
        )
        r_hot_mirror = _insert_raw_item(
            conn,
            source_id=s_nyt,
            url="https://nytimes.com/2026/07/gpt5",
            title="NYT covers GPT-5",
        )
        c_hot = _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
            category="models",
        )

        # Older, weakly-sourced models story — expected #2 in models.
        r_cold = _insert_raw_item(
            conn,
            source_id=s_random,
            url="https://random.example.com/notes",
            title="Some notes",
            first_seen_at=NOW - timedelta(hours=96),
        )
        c_cold = _insert_cluster(
            conn,
            primary_url="https://random.example.com/notes",
            canonical_headline="Some notes",
            category="models",
        )

        # A funding story to prove per-category bucketing works end-to-end.
        r_fund = _insert_raw_item(
            conn,
            source_id=s_nyt,
            url="https://nytimes.com/2026/07/round",
            title="Anthropic raises Series F at $60B valuation",
        )
        c_fund = _insert_cluster(
            conn,
            primary_url="https://nytimes.com/2026/07/round",
            canonical_headline="Anthropic raises Series F at $60B valuation",
            category="funding",
        )

        assignments = {
            r_hot: c_hot,
            r_hot_mirror: c_hot,
            r_cold: c_cold,
            r_fund: c_fund,
        }

    with curated_engine.begin() as conn:
        buckets = rank_clusters_from_db(conn, now=NOW, assignments=assignments)

    assert [r.cluster_id for r in buckets["models"]] == [c_hot, c_cold]
    assert [r.cluster_id for r in buckets["funding"]] == [c_fund]
    assert buckets["research"] == []
    assert buckets["lawsuits_policy"] == []
    assert buckets["industry_moves"] == []
    # And every category appears.
    assert set(buckets.keys()) == set(CATEGORIES)


def test_rank_clusters_from_db_respects_custom_weights(
    curated_engine: Engine,
) -> None:
    """Shrinking the half-life makes older stories decay faster."""
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        r = _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/p1",
            title="OpenAI unveils GPT-5",
            first_seen_at=NOW - timedelta(hours=24),
        )
        c = _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/p1",
            canonical_headline="OpenAI unveils GPT-5",
        )
        assignments = {r: c}

    with curated_engine.begin() as conn:
        default = rank_clusters_from_db(conn, now=NOW, assignments=assignments)
    with curated_engine.begin() as conn:
        aggressive = rank_clusters_from_db(
            conn,
            now=NOW,
            assignments=assignments,
            weights=RankingWeights(recency_half_life_hours=6),
        )

    assert aggressive["models"][0].recency < default["models"][0].recency
