"""Tests for :mod:`signalweek.digest.builder`.

The builder wraps the last step of the pipeline: given the DB state produced
by ingest → cluster, it classifies, ranks, dedups against recent issues, and
writes an ``issues`` row plus its ``items``. Tests are structured around the
guarantees the task calls out:

* Only the last 7 days of clusters feed a build.
* Every candidate is filtered against the last 12 *published* issues.
* Items are ordered by the fixed 5-section taxonomy, with descending rank
  inside each section.
* Fewer than 10 items → held (never published), otherwise published with a
  ``published_at`` timestamp.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.digest.builder import (
    DEFAULT_MIN_ITEMS,
    WHAT_HAPPENED_MAX_WORDS,
    BuildResult,
    IssueAlreadyExistsError,
    build_issue,
    build_what_happened,
    refresh_item_render_fields,
)
from signalweek.ingest.classify import CATEGORIES
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)  # a Monday
MONDAY = NOW.date()


# ---------------------------------------------------------------------------
# Fixtures + seeders
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
    name: str | None = None,
) -> int:
    result = conn.execute(
        sources_table.insert()
        .values(url=url, kind="rss", category_hint=category_hint, active=True, name=name)
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
    body: str | None = None,
    first_seen_at: datetime = NOW,
    published_at: datetime | None = None,
) -> int:
    result = conn.execute(
        raw_items_table.insert()
        .values(
            source_id=source_id,
            url=url,
            canonical_url=canonical_url or url,
            title=title,
            body=body,
            fetched_at=first_seen_at,
            first_seen_at=first_seen_at,
            published_at=published_at,
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


def _insert_published_issue(
    conn: Connection,
    *,
    week_of: date,
    published_at: datetime,
    primary_urls: list[str],
    starting_position: int = 1,
) -> int:
    """Seed a past issue with items pointing at ``primary_urls``.

    Each URL gets an ephemeral cluster row (dedup only reads ``primary_url``
    from ``items``, but ``items`` still has an FK to ``clusters``).
    """
    result = conn.execute(
        issues_table.insert()
        .values(week_of=week_of, status="published", published_at=published_at)
        .returning(issues_table.c.id)
    )
    issue_id = int(result.scalar_one())
    for offset, url in enumerate(primary_urls):
        cid = _insert_cluster(conn, primary_url=url, canonical_headline=f"Old story {url}")
        conn.execute(
            items_table.insert().values(
                issue_id=issue_id,
                cluster_id=cid,
                category="models",
                position=starting_position + offset,
                headline=f"Old story {url}",
                summary=f"Old story {url}",
                primary_url=url,
                extra_source_urls=[],
            )
        )
    return issue_id


def _seed_full_issue_of_candidates(conn: Connection) -> None:
    """Seed a well-distributed batch of recent clusters that will comfortably
    exceed :data:`DEFAULT_MIN_ITEMS` — used by the happy-path publish test."""
    s = _insert_source(conn, url="https://openai.com/blog/rss.xml")

    # 3 models, 2 funding, 2 lawsuits_policy, 2 research, 2 industry_moves = 11
    seeds = [
        ("models", "OpenAI unveils GPT-5", "https://openai.com/blog/gpt-5"),
        ("models", "Anthropic releases Claude 5", "https://openai.com/blog/claude-5"),
        ("models", "Google launches Gemini 3", "https://openai.com/blog/gemini-3"),
        (
            "funding",
            "Anthropic raises Series F at $60B valuation",
            "https://openai.com/blog/anthropic-round",
        ),
        (
            "funding",
            "Startup raises $200M Series C",
            "https://openai.com/blog/startup-round",
        ),
        (
            "lawsuits_policy",
            "Court rules against major AI vendor",
            "https://openai.com/blog/court-ruling",
        ),
        (
            "lawsuits_policy",
            "Executive order signed on AI safety",
            "https://openai.com/blog/eo-ai",
        ),
        (
            "research",
            "New preprint proposes novel benchmark",
            "https://openai.com/blog/preprint",
        ),
        (
            "research",
            "Researchers report SOTA on reasoning benchmark",
            "https://openai.com/blog/sota",
        ),
        (
            "industry_moves",
            "Google hires new CEO for AI division",
            "https://openai.com/blog/hire-ceo",
        ),
        (
            "industry_moves",
            "Meta announces layoffs across AI org",
            "https://openai.com/blog/layoffs",
        ),
    ]
    for category, headline, url in seeds:
        _insert_raw_item(
            conn,
            source_id=s,
            url=url,
            title=headline,
            body=f"{headline}. Extended context follows this lede for the item summary.",
        )
        _insert_cluster(conn, primary_url=url, canonical_headline=headline, category=category)


# ---------------------------------------------------------------------------
# Basic behaviour: empty, week_of, uniqueness
# ---------------------------------------------------------------------------


def test_build_with_no_clusters_holds_an_empty_issue(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert isinstance(result, BuildResult)
    assert result.status == "held"
    assert result.total_items == 0
    assert result.week_of == MONDAY

    with curated_engine.begin() as conn:
        row = conn.execute(issues_table.select().where(issues_table.c.id == result.issue_id)).one()
    assert row.status == "held"
    assert row.published_at is None


def test_week_of_defaults_to_monday_of_now(curated_engine: Engine) -> None:
    # A Thursday: the ISO week starts on Monday, 3 days earlier.
    thursday = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
    with curated_engine.begin() as conn:
        result = build_issue(conn, now=thursday)
    assert result.week_of == date(2026, 7, 27)


def test_week_of_can_be_overridden(curated_engine: Engine) -> None:
    override = date(2026, 6, 1)
    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, week_of=override)
    assert result.week_of == override


def test_rebuilding_the_same_week_raises(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        build_issue(conn, now=NOW)
    with curated_engine.begin() as conn, pytest.raises(IssueAlreadyExistsError):
        build_issue(conn, now=NOW)


# ---------------------------------------------------------------------------
# Lookback window
# ---------------------------------------------------------------------------


def test_clusters_outside_the_7_day_window_are_ignored(curated_engine: Engine) -> None:
    """A cluster whose raw_items are all older than 7 days must not appear."""
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/old",
            title="OpenAI unveils old GPT model",
            first_seen_at=NOW - timedelta(days=30),
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/old",
            canonical_headline="OpenAI unveils old GPT model",
            category="models",
        )
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/fresh",
            title="OpenAI unveils fresh GPT model",
            first_seen_at=NOW - timedelta(days=1),
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/fresh",
            canonical_headline="OpenAI unveils fresh GPT model",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.candidates_considered == 1
    assert result.total_items == 1
    with curated_engine.begin() as conn:
        rows = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).all()
    assert [r.headline for r in rows] == ["OpenAI unveils fresh GPT model"]


# ---------------------------------------------------------------------------
# Publish vs hold guard
# ---------------------------------------------------------------------------


def test_full_issue_is_published_with_timestamp(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        _seed_full_issue_of_candidates(conn)

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.status == "published"
    assert result.total_items >= DEFAULT_MIN_ITEMS

    with curated_engine.begin() as conn:
        row = conn.execute(issues_table.select().where(issues_table.c.id == result.issue_id)).one()
    assert row.status == "published"
    assert row.published_at is not None


def test_thin_issue_is_held_and_carries_no_published_at(curated_engine: Engine) -> None:
    """Fewer than DEFAULT_MIN_ITEMS across all categories → held."""
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        # Only 3 items — nowhere near the 10-item floor.
        for idx in range(3):
            url = f"https://openai.com/blog/story-{idx}"
            _insert_raw_item(
                conn,
                source_id=s,
                url=url,
                title=f"OpenAI unveils model {idx}",
                first_seen_at=NOW - timedelta(hours=idx),
            )
            _insert_cluster(
                conn,
                primary_url=url,
                canonical_headline=f"OpenAI unveils model {idx}",
                category="models",
            )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.status == "held"
    assert result.total_items == 3
    with curated_engine.begin() as conn:
        row = conn.execute(issues_table.select().where(issues_table.c.id == result.issue_id)).one()
    assert row.status == "held"
    assert row.published_at is None
    # Held issues still materialise their items — an editor can inspect them.
    with curated_engine.begin() as conn:
        item_count = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).all()
    assert len(item_count) == 3


def test_min_items_is_configurable(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        for idx in range(3):
            url = f"https://openai.com/blog/story-{idx}"
            _insert_raw_item(conn, source_id=s, url=url, title=f"OpenAI unveils model {idx}")
            _insert_cluster(
                conn,
                primary_url=url,
                canonical_headline=f"OpenAI unveils model {idx}",
                category="models",
            )
    # With min_items=3, three items are enough to publish.
    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=3)
    assert result.status == "published"


# ---------------------------------------------------------------------------
# Ordering: fixed 5-section taxonomy
# ---------------------------------------------------------------------------


def test_items_are_ordered_by_the_fixed_5_section_taxonomy(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        _seed_full_issue_of_candidates(conn)

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)
        rows = conn.execute(
            items_table.select()
            .where(items_table.c.issue_id == result.issue_id)
            .order_by(items_table.c.position)
        ).all()

    ordered_categories = [r.category for r in rows]
    # The section boundaries follow the fixed CATEGORIES order.
    seen_first_index = {
        cat: ordered_categories.index(cat) for cat in CATEGORIES if cat in ordered_categories
    }
    fixed_order = [c for c in CATEGORIES if c in seen_first_index]
    assert list(seen_first_index.keys()) == fixed_order
    # Positions are 1..N with no gaps.
    assert [r.position for r in rows] == list(range(1, len(rows) + 1))


def test_top_n_per_category_caps_each_section(curated_engine: Engine) -> None:
    """With N=2, no category may have more than 2 items even if 5 fit."""
    with curated_engine.begin() as conn:
        _seed_full_issue_of_candidates(conn)

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, top_n_per_category=2, min_items=1)

    for count in result.items_per_category.values():
        assert count <= 2


# ---------------------------------------------------------------------------
# 12-week cross-issue URL dedup
# ---------------------------------------------------------------------------


def test_cluster_matching_a_recent_published_issue_url_is_rejected(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        _insert_published_issue(
            conn,
            week_of=MONDAY - timedelta(weeks=1),
            published_at=NOW - timedelta(weeks=1),
            primary_urls=["https://openai.com/blog/gpt-5"],
        )
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.rejected_by_dedup == 1
    assert result.total_items == 0


def test_dedup_uses_canonical_urls_not_raw_urls(curated_engine: Engine) -> None:
    """Tracking parameters and case differences must not evade the dedup."""
    with curated_engine.begin() as conn:
        _insert_published_issue(
            conn,
            week_of=MONDAY - timedelta(weeks=2),
            published_at=NOW - timedelta(weeks=2),
            primary_urls=["https://openai.com/blog/gpt-5"],
        )
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        # Same story, published URL carries a UTM tag — the ingest step
        # canonicalizes it away before it lands in raw_items.canonical_url.
        variant_url = "https://openai.com/blog/gpt-5?utm_source=rss&utm_campaign=x"
        _insert_raw_item(
            conn,
            source_id=s,
            url=variant_url,
            canonical_url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
        )
        _insert_cluster(
            conn,
            primary_url=variant_url,
            canonical_headline="OpenAI unveils GPT-5",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.rejected_by_dedup == 1
    assert result.total_items == 0


def test_dedup_only_considers_published_issues(curated_engine: Engine) -> None:
    """A held or draft prior issue must not consume the dedup budget."""
    with curated_engine.begin() as conn:
        # Seed a "held" prior issue that mentions the URL — should NOT block.
        result = conn.execute(
            issues_table.insert()
            .values(
                week_of=MONDAY - timedelta(weeks=1),
                status="held",
                published_at=None,
            )
            .returning(issues_table.c.id)
        )
        held_issue_id = int(result.scalar_one())
        held_cluster_id = _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
        )
        conn.execute(
            items_table.insert().values(
                issue_id=held_issue_id,
                cluster_id=held_cluster_id,
                category="models",
                position=1,
                headline="OpenAI unveils GPT-5",
                summary="OpenAI unveils GPT-5",
                primary_url="https://openai.com/blog/gpt-5",
                extra_source_urls=[],
            )
        )

        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/new-story",
            title="OpenAI unveils GPT-5",
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/new-story",
            canonical_headline="OpenAI unveils GPT-5",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.rejected_by_dedup == 0
    assert result.total_items == 1


def test_dedup_window_only_looks_back_12_issues(curated_engine: Engine) -> None:
    """A URL from the 13th-most-recent published issue does not block."""
    with curated_engine.begin() as conn:
        # Oldest issue (13 weeks ago) — carries the URL that will *not* block.
        _insert_published_issue(
            conn,
            week_of=MONDAY - timedelta(weeks=13),
            published_at=NOW - timedelta(weeks=13),
            primary_urls=["https://openai.com/blog/very-old"],
        )
        # 12 more recent published issues, each with a distinct dummy URL so
        # they collectively fill the dedup window without ever touching the
        # URL under test.
        for w in range(1, 13):
            _insert_published_issue(
                conn,
                week_of=MONDAY - timedelta(weeks=w),
                published_at=NOW - timedelta(weeks=w),
                primary_urls=[f"https://example.com/filler-{w}"],
            )

        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/very-old",
            title="OpenAI unveils very-old model",
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/very-old",
            canonical_headline="OpenAI unveils very-old model",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    # Only the 12 more-recent issues participate — the 13-week-old URL slips
    # out of the dedup window and is admitted.
    assert result.rejected_by_dedup == 0
    assert result.total_items == 1


# ---------------------------------------------------------------------------
# Rule-based extractive summary + extra sources
# ---------------------------------------------------------------------------


def test_summary_extracts_a_short_lede_and_strips_html(curated_engine: Engine) -> None:
    body = (
        "<p>OpenAI unveils GPT-5, a next-generation reasoning model. "
        "The company says the new system doubles benchmark scores over GPT-4. "
        "More details will follow in a live-streamed briefing.</p>"
    )
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
            body=body,
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    assert row.headline == "OpenAI unveils GPT-5"
    # The sentence repeating the headline goes; HTML is stripped.
    assert row.summary == (
        "The company says the new system doubles benchmark scores over GPT-4. "
        "More details will follow in a live-streamed briefing."
    )


def test_summary_is_empty_when_body_is_empty(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
            body=None,
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    # No body: no what-happened text rather than a repeat of the headline.
    assert row.summary == ""


def test_extra_source_urls_collects_distinct_additional_outlets(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        s_openai = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        s_nyt = _insert_source(conn, url="https://nytimes.com/rss.xml")
        s_verge = _insert_source(conn, url="https://theverge.com/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s_openai,
            url="https://openai.com/blog/gpt-5",
            canonical_url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
            body="OpenAI unveils GPT-5.",
        )
        _insert_raw_item(
            conn,
            source_id=s_nyt,
            url="https://openai.com/blog/gpt-5?utm_source=nyt",
            canonical_url="https://openai.com/blog/gpt-5",
            title="NYT covers GPT-5",
        )
        _insert_raw_item(
            conn,
            source_id=s_verge,
            url="https://openai.com/blog/gpt-5?utm_source=verge",
            canonical_url="https://openai.com/blog/gpt-5",
            title="Verge covers GPT-5",
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    # Primary outlet (openai.com) is excluded; the two other outlets are kept
    # in deterministic order.
    assert row.extra_source_urls == [
        "https://nytimes.com/rss.xml",
        "https://theverge.com/rss.xml",
    ]


def test_arxiv_extra_sources_are_dropped_outside_research(curated_engine: Engine) -> None:
    """AIC-12: no arXiv URL may appear in a non-Research section, citations included."""
    with curated_engine.begin() as conn:
        s_openai = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        s_arxiv = _insert_source(conn, url="https://arxiv.org/rss/cs.CL", category_hint="research")
        s_verge = _insert_source(conn, url="https://theverge.com/rss.xml")
        for source_id, suffix in ((s_openai, ""), (s_arxiv, "?a"), (s_verge, "?v")):
            _insert_raw_item(
                conn,
                source_id=source_id,
                url=f"https://openai.com/blog/gpt-5{suffix}",
                canonical_url="https://openai.com/blog/gpt-5",
                title="OpenAI unveils GPT-5",
            )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    assert row.category == "models"
    assert row.extra_source_urls == ["https://theverge.com/rss.xml"]


# ---------------------------------------------------------------------------
# Render contract (spec criterion 8): byline + <=40-word what-happened
# ---------------------------------------------------------------------------

ARXIV_TITLE = "OneBid: A Unified Auto-Bidding Foundation Model for Diverse oCPX Advertising"
ARXIV_BODY = (
    "arXiv:2609.21550v1 Announce Type: cross Abstract: Auto-bidding is central to "
    "online advertising. We present OneBid, a single foundation model that serves "
    "every oCPX scenario at once. It replaces dozens of per-scenario models, cuts "
    "serving cost by forty percent and lifts advertiser value in large online A/B "
    "tests across three markets. We release the training recipe and an offline "
    "benchmark so others can reproduce the results."
)


def test_what_happened_strips_arxiv_preamble_and_caps_at_40_words() -> None:
    text = build_what_happened(ARXIV_TITLE, ARXIV_BODY)
    assert text.startswith("Auto-bidding is central to online advertising.")
    assert "arXiv:" not in text and "Abstract:" not in text
    assert len(text.split()) <= WHAT_HAPPENED_MAX_WORDS
    # Whole sentences only when they fit.
    assert text.endswith(".")


def test_what_happened_strips_a_repeated_headline_prefix() -> None:
    stored = f"{ARXIV_TITLE} — {ARXIV_BODY[:200]}"
    text = build_what_happened(ARXIV_TITLE, stored)
    assert not text.startswith("OneBid")
    assert text.startswith("Auto-bidding")


def test_what_happened_cuts_one_long_sentence_at_the_word_budget() -> None:
    body = "Word " * 90 + "end."
    text = build_what_happened("Headline", body)
    assert len(text.split()) == WHAT_HAPPENED_MAX_WORDS
    assert text.endswith("…")


def test_what_happened_drops_wordpress_footer_and_entities() -> None:
    body = (
        "<p>Acme raised &#36;200M &amp; more.</p> "
        "The post Acme raises $200M appeared first on Example News."
    )
    assert build_what_happened("Acme raises $200M", body) == "Acme raised $200M & more."


def test_headline_entities_are_decoded_on_build(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://www.theverge.com/rss/ai/index.xml")
        _insert_raw_item(conn, source_id=s, url="https://www.theverge.com/a", body="Body.")
        _insert_cluster(
            conn,
            primary_url="https://www.theverge.com/a",
            canonical_headline="Nvidia&#8217;s Jensen Huang speaks",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    assert row.headline == "Nvidia\u2019s Jensen Huang speaks"


def test_what_happened_is_empty_when_body_only_repeats_the_headline() -> None:
    assert build_what_happened("OpenAI unveils GPT-5", "OpenAI unveils GPT-5.") == ""


def test_item_stores_source_name_and_entry_publish_date(curated_engine: Engine) -> None:
    published = datetime(2026, 7, 25, 14, 30, tzinfo=UTC)
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml", name="OpenAI")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://openai.com/blog/gpt-5",
            title="OpenAI unveils GPT-5",
            body="GPT-5 ships today.",
            published_at=published,
        )
        _insert_cluster(
            conn,
            primary_url="https://openai.com/blog/gpt-5",
            canonical_headline="OpenAI unveils GPT-5",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    assert row.source_name == "OpenAI"
    assert row.source_published_at == published.replace(tzinfo=None)


def test_item_byline_falls_back_to_host_and_first_seen(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://feeds.example.com/rss")
        _insert_raw_item(conn, source_id=s, url="https://www.example.com/story", body="x.")
        _insert_cluster(
            conn,
            primary_url="https://www.example.com/story",
            canonical_headline="Story",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = build_issue(conn, now=NOW, min_items=1)
        row = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).one()

    assert row.source_name == "example.com"
    assert row.source_published_at == NOW.replace(tzinfo=None)


def test_refresh_item_render_fields_repairs_already_built_items(curated_engine: Engine) -> None:
    published = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
    with curated_engine.begin() as conn:
        s = _insert_source(conn, url="https://export.arxiv.org/rss/cs.AI", name="arXiv cs.AI")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://arxiv.org/abs/2609.21550",
            title=ARXIV_TITLE,
            body=ARXIV_BODY,
            published_at=published,
        )
        cid = _insert_cluster(
            conn,
            primary_url="https://arxiv.org/abs/2609.21550",
            canonical_headline=ARXIV_TITLE,
            category="research",
        )
        issue_id = int(
            conn.execute(
                issues_table.insert()
                .values(week_of=date(2026, 7, 20), status="published", published_at=NOW)
                .returning(issues_table.c.id)
            ).scalar_one()
        )
        conn.execute(
            items_table.insert().values(
                issue_id=issue_id,
                cluster_id=cid,
                category="research",
                position=1,
                headline=ARXIV_TITLE,
                summary=f"{ARXIV_TITLE} — {ARXIV_BODY}",
                primary_url="https://arxiv.org/abs/2609.21550",
                extra_source_urls=[],
            )
        )

    with curated_engine.begin() as conn:
        assert refresh_item_render_fields(conn) == 1
        row = conn.execute(items_table.select()).one()

    assert row.source_name == "arXiv cs.AI"
    assert row.source_published_at == published.replace(tzinfo=None)
    assert row.summary.startswith("Auto-bidding")
    assert len(row.summary.split()) <= WHAT_HAPPENED_MAX_WORDS
