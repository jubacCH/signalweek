"""Tests for :mod:`signalweek.ingest.classify`.

Split into two parts:

* Pure classifier tests exercise :func:`classify_text` — one category at a
  time, then tie-breaking and fallback behaviour.
* Integration tests build a fresh in-memory SQLite with the curated tables,
  seed sources/raw_items/clusters directly, and assert that
  :func:`classify_clusters` writes the expected ``clusters.category`` values.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest.classify import (
    CATEGORIES,
    CATEGORY_LABELS,
    FALLBACK_CATEGORY,
    classify_clusters,
    classify_text,
)
from signalweek.sources import (
    clusters_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)

BASE_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


def test_categories_are_the_fixed_five() -> None:
    """The taxonomy is exactly the five approved buckets — no extras, no gaps."""
    assert set(CATEGORIES) == {
        "models",
        "lawsuits_policy",
        "funding",
        "research",
        "industry_moves",
    }
    assert set(CATEGORY_LABELS.keys()) == set(CATEGORIES)
    assert CATEGORY_LABELS["lawsuits_policy"] == "Lawsuits & policy"
    assert CATEGORY_LABELS["industry_moves"] == "Industry moves"


def test_no_uncategorized_bucket() -> None:
    assert "uncategorized" not in CATEGORIES
    assert FALLBACK_CATEGORY in CATEGORIES


def test_classify_text_always_returns_one_of_the_five_categories() -> None:
    """Whatever the input, the result is a valid category — never a new bucket."""
    for text in [
        "",
        "totally unrelated text",
        "OpenAI ships GPT-5",
        "European Commission fines company",
        "Anthropic raises Series F",
    ]:
        assert classify_text(text) in CATEGORIES


def test_classify_headline_about_a_new_model_returns_models() -> None:
    assert classify_text("OpenAI unveils GPT-5 with a bigger context window") == "models"
    assert classify_text("Meta open-sources Llama 4 with new weights") == "models"


def test_classify_headline_about_a_lawsuit_returns_lawsuits_policy() -> None:
    assert (
        classify_text("New York Times sues OpenAI over copyright infringement") == "lawsuits_policy"
    )


def test_classify_headline_about_regulation_returns_lawsuits_policy() -> None:
    assert classify_text("European Commission proposes new AI Act amendments") == "lawsuits_policy"


def test_classify_headline_about_a_funding_round_returns_funding() -> None:
    assert classify_text("Anthropic raises Series F at a $60B valuation") == "funding"
    assert classify_text("Nvidia acquires Run:ai in $700M acquisition") == "funding"


def test_classify_headline_about_a_paper_returns_research() -> None:
    assert classify_text("New arXiv paper proposes a novel benchmark for reasoning") == "research"


def test_classify_headline_about_a_hire_returns_industry_moves() -> None:
    assert classify_text("Google hires former Apple CTO to lead AI hardware") == "industry_moves"


def test_hint_used_when_no_keyword_matches() -> None:
    """A vague headline with no lexicon hits falls back to the source hint."""
    assert classify_text("Weekly notes and thoughts", category_hint="research") == "research"
    assert classify_text("Weekly notes and thoughts", category_hint="funding") == "funding"


def test_fallback_when_no_keywords_and_no_hint() -> None:
    """No signal at all lands the cluster in the broad industry_moves bucket."""
    assert classify_text("Weekly notes and thoughts") == FALLBACK_CATEGORY
    assert classify_text("Weekly notes and thoughts") == "industry_moves"


def test_invalid_hint_is_ignored_and_falls_back() -> None:
    assert (
        classify_text("Weekly notes and thoughts", category_hint="uncategorized")
        == FALLBACK_CATEGORY
    )
    assert classify_text("A ruling was issued", category_hint="bogus") == "lawsuits_policy"


def test_keywords_can_override_the_source_hint() -> None:
    """A clearly-legal story from an industry-moves source lands in lawsuits_policy."""
    text = "OpenAI sued in class action over copyright infringement"
    assert classify_text(text, category_hint="industry_moves") == "lawsuits_policy"


def test_hint_wins_tie_when_present_in_top_scorers() -> None:
    """One keyword hit each for two categories — the source hint breaks the tie."""
    # "release" is a models term, "acquires" is a funding term — one match each.
    text = "The company releases and acquires assets"
    assert classify_text(text, category_hint="models") == "models"
    assert classify_text(text, category_hint="funding") == "funding"


def test_fixed_priority_used_when_hint_not_in_top_scorers() -> None:
    """When the hint is unrelated to a tie, the fixed priority order decides."""
    text = "The company releases and acquires assets"
    # Neither category matches the hint; lawsuits_policy > funding > models
    # in the tiebreak order, so between funding and models funding wins.
    assert classify_text(text, category_hint="research") == "funding"


def test_matching_is_case_insensitive_and_word_bounded() -> None:
    # Word-bounded: "release" appears in "releases" (bounded on either side).
    assert classify_text("Team RELEASES a new checkpoint") == "models"
    # But it does not match inside an unrelated word.
    # "vcs" (as in "version control") should not trigger the funding "vc" term.
    assert classify_text("A new post about vcs and workflows") != "funding"


def test_classify_text_is_deterministic_on_repeated_calls() -> None:
    text = "Anthropic raises Series F at a $60B valuation"
    outputs = {classify_text(text, category_hint="funding") for _ in range(10)}
    assert outputs == {"funding"}


# ---------------------------------------------------------------------------
# classify_clusters (DB integration)
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
    category_hint: str | None = "industry_moves",
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
    title: str,
) -> int:
    result = conn.execute(
        raw_items_table.insert()
        .values(
            source_id=source_id,
            url=url,
            canonical_url=canonical_url or url,
            title=title,
            body=None,
            fetched_at=BASE_TIME,
            first_seen_at=BASE_TIME,
        )
        .returning(raw_items_table.c.id)
    )
    return int(result.scalar_one())


def _insert_cluster(
    conn: Connection,
    *,
    primary_url: str,
    canonical_headline: str,
    category: str,
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


def _cluster_categories(conn: Connection) -> dict[int, str]:
    stmt = select(clusters_table.c.id, clusters_table.c.category).order_by(clusters_table.c.id)
    return {int(row.id): row.category for row in conn.execute(stmt).all()}


def test_classify_clusters_with_no_clusters_does_nothing(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        result = classify_clusters(conn)
    assert result.total == 0
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.categories == {}


def test_classify_clusters_uses_source_hint_when_headline_is_vague(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(
            conn, url="https://labs.example.com/feed", category_hint="models"
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://labs.example.com/posts/notes",
            title="Weekly notes and thoughts",
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://labs.example.com/posts/notes",
            canonical_headline="Weekly notes and thoughts",
            # Simulate a cluster that was mis-seeded — the classify pass
            # should correct it to match the source hint.
            category="industry_moves",
        )

    with curated_engine.begin() as conn:
        result = classify_clusters(conn)
        stored = _cluster_categories(conn)

    assert result.updated == 1
    assert result.unchanged == 0
    assert result.categories == {cluster_id: "models"}
    assert stored == {cluster_id: "models"}


def test_classify_clusters_overrides_hint_when_keywords_disagree(
    curated_engine: Engine,
) -> None:
    """A tech-press source (industry_moves) covering a lawsuit lands in lawsuits_policy."""
    with curated_engine.begin() as conn:
        source_id = _insert_source(
            conn, url="https://verge.example.com/feed", category_hint="industry_moves"
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://verge.example.com/posts/suit",
            title="OpenAI sued in class action over copyright infringement",
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://verge.example.com/posts/suit",
            canonical_headline="OpenAI sued in class action over copyright infringement",
            category="industry_moves",
        )

    with curated_engine.begin() as conn:
        result = classify_clusters(conn)
        stored = _cluster_categories(conn)

    assert stored[cluster_id] == "lawsuits_policy"
    assert result.updated == 1
    assert result.unchanged == 0


def test_classify_clusters_leaves_correctly_categorised_rows_untouched(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        source_id = _insert_source(
            conn, url="https://blog.example.com/feed", category_hint="models"
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://blog.example.com/posts/gpt5",
            title="OpenAI unveils GPT-5 with reasoning improvements",
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://blog.example.com/posts/gpt5",
            canonical_headline="OpenAI unveils GPT-5 with reasoning improvements",
            category="models",
        )

    with curated_engine.begin() as conn:
        result = classify_clusters(conn)
        stored = _cluster_categories(conn)

    assert result.updated == 0
    assert result.unchanged == 1
    assert stored[cluster_id] == "models"


def test_classify_clusters_processes_a_mixed_batch(curated_engine: Engine) -> None:
    """Every cluster ends up in exactly one of the five buckets — never Uncategorized."""
    with curated_engine.begin() as conn:
        s_models = _insert_source(conn, url="https://labs.example.com/feed", category_hint="models")
        s_verge = _insert_source(
            conn, url="https://verge.example.com/feed", category_hint="industry_moves"
        )
        s_tc = _insert_source(conn, url="https://tc.example.com/feed", category_hint="funding")
        s_arxiv = _insert_source(
            conn, url="https://arxiv.example.com/feed", category_hint="research"
        )
        s_ftc = _insert_source(
            conn, url="https://ftc.example.com/feed", category_hint="lawsuits_policy"
        )

        fixtures = [
            (s_models, "https://labs.example.com/p/gpt5", "OpenAI unveils GPT-5", "models"),
            (
                s_verge,
                "https://verge.example.com/p/hire",
                "Google hires former Apple CTO to lead AI hardware",
                "industry_moves",
            ),
            (
                s_tc,
                "https://tc.example.com/p/round",
                "Anthropic raises Series F at $60B valuation",
                "funding",
            ),
            (
                s_arxiv,
                "https://arxiv.example.com/p/paper",
                "New arXiv preprint proposes a benchmark for reasoning",
                "research",
            ),
            (
                s_ftc,
                "https://ftc.example.com/p/order",
                "FTC issues executive order on AI safety",
                "lawsuits_policy",
            ),
        ]

        expected: dict[int, str] = {}
        for source_id, url, title, expected_cat in fixtures:
            _insert_raw_item(conn, source_id=source_id, url=url, title=title)
            # Seed every cluster with the same (wrong) starting category to
            # prove the pass classifies from scratch rather than reading it.
            cid = _insert_cluster(
                conn,
                primary_url=url,
                canonical_headline=title,
                category="industry_moves",
            )
            expected[cid] = expected_cat

    with curated_engine.begin() as conn:
        result = classify_clusters(conn)
        stored = _cluster_categories(conn)

    assert stored == expected
    # Every cluster ends up in one of the five buckets — no Uncategorized.
    assert all(cat in CATEGORIES for cat in stored.values())
    assert result.total == len(fixtures)


def test_classify_clusters_is_idempotent(curated_engine: Engine) -> None:
    """Running the pass twice produces the same categories the second time."""
    with curated_engine.begin() as conn:
        source_id = _insert_source(
            conn, url="https://verge.example.com/feed", category_hint="industry_moves"
        )
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://verge.example.com/p/suit",
            title="OpenAI sued in class action over copyright infringement",
        )
        _insert_cluster(
            conn,
            primary_url="https://verge.example.com/p/suit",
            canonical_headline="OpenAI sued in class action over copyright infringement",
            category="industry_moves",
        )

    with curated_engine.begin() as conn:
        first = classify_clusters(conn)
    with curated_engine.begin() as conn:
        second = classify_clusters(conn)
        stored = _cluster_categories(conn)

    assert first.updated == 1
    assert first.unchanged == 0
    assert second.updated == 0
    assert second.unchanged == 1
    assert set(stored.values()) == {"lawsuits_policy"}


def test_classify_clusters_without_source_hint_still_lands_in_valid_bucket(
    curated_engine: Engine,
) -> None:
    """A cluster whose source has no hint and whose headline is vague still classifies."""
    with curated_engine.begin() as conn:
        source_id = _insert_source(conn, url="https://misc.example.com/feed", category_hint=None)
        _insert_raw_item(
            conn,
            source_id=source_id,
            url="https://misc.example.com/p/notes",
            title="A short update",
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://misc.example.com/p/notes",
            canonical_headline="A short update",
            category="industry_moves",
        )

    with curated_engine.begin() as conn:
        classify_clusters(conn)
        stored = _cluster_categories(conn)

    assert stored[cluster_id] in CATEGORIES
    assert stored[cluster_id] == FALLBACK_CATEGORY


def test_classify_clusters_handles_missing_anchor_raw_item(
    curated_engine: Engine,
) -> None:
    """When no raw_item matches primary_url, the current category serves as the hint."""
    with curated_engine.begin() as conn:
        # No raw_items or sources at all — cluster is orphaned.
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://orphan.example.com/p/x",
            canonical_headline="Weekly notes and thoughts",
            category="research",
        )

    with curated_engine.begin() as conn:
        result = classify_clusters(conn)
        stored = _cluster_categories(conn)

    # No keyword hits → fall back to the stored category as the "hint".
    assert stored[cluster_id] == "research"
    assert result.unchanged == 1
