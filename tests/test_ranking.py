"""Tabular unit tests for the ranking module.

Each behaviour of :mod:`signalweek.ranking` is exercised through
``pytest.mark.parametrize`` fixtures so the expected shape of the score is
readable at a glance and easy to extend.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from signalweek.ranking import (
    RankableSignal,
    RankedSignal,
    RankingWeights,
    engagement_score,
    keyword_score,
    rank_signals,
    recency_score,
    score_signal,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# recency_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_hours", "half_life_hours", "expected"),
    [
        (0.0, 24.0, 1.0),
        (24.0, 24.0, 0.5),
        (48.0, 24.0, 0.25),
        (72.0, 24.0, 0.125),
        (12.0, 24.0, math.sqrt(0.5)),
        (48.0, 48.0, 0.5),
        (-1.0, 24.0, 1.0),  # future-dated items clamp to full score
    ],
)
def test_recency_score_halves_every_half_life(
    age_hours: float, half_life_hours: float, expected: float
) -> None:
    published = NOW - timedelta(hours=age_hours)
    got = recency_score(published, now=NOW, half_life_hours=half_life_hours)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    ("published_at", "half_life_hours"),
    [
        (None, 24.0),
        (NOW, 0.0),
        (NOW, -1.0),
    ],
)
def test_recency_score_zero_when_undefined_or_no_decay(
    published_at: datetime | None, half_life_hours: float
) -> None:
    assert recency_score(published_at, now=NOW, half_life_hours=half_life_hours) == 0.0


# ---------------------------------------------------------------------------
# engagement_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engagement", "saturation", "expected"),
    [
        (0.0, 100.0, 0.0),
        (-5.0, 100.0, 0.0),
        (100.0, 100.0, 1.0),
        (1000.0, 100.0, 1.0),  # log-compressed but clipped at saturation
        (10.0, 100.0, math.log1p(10.0) / math.log1p(100.0)),
        (50.0, 50.0, 1.0),
    ],
)
def test_engagement_score_log_compresses_and_saturates(
    engagement: float, saturation: float, expected: float
) -> None:
    got = engagement_score(engagement, saturation=saturation)
    assert got == pytest.approx(expected)


def test_engagement_score_zero_when_saturation_non_positive() -> None:
    assert engagement_score(10.0, saturation=0.0) == 0.0
    assert engagement_score(10.0, saturation=-1.0) == 0.0


# ---------------------------------------------------------------------------
# keyword_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "keywords", "expected"),
    [
        # Case-insensitive whole-word match.
        ("Rust is fast", {"rust": 2.0}, 2.0),
        ("RUST is fast", {"rust": 2.0}, 2.0),
        # Whole-word: 'ai' inside 'chair' does NOT match.
        ("A comfortable chair", {"ai": 5.0}, 0.0),
        ("Everything about AI", {"ai": 5.0}, 5.0),
        # Multiple keywords sum.
        ("Rust and Go benchmarks", {"rust": 1.5, "go": 0.5}, 2.0),
        # A keyword counts once regardless of occurrences.
        ("Rust rust RUST rust", {"rust": 1.0}, 1.0),
        # No keywords -> zero, no crash.
        ("anything", {}, 0.0),
        # Empty/whitespace keys are ignored.
        ("Rust", {" ": 10.0, "": 5.0, "rust": 1.0}, 1.0),
    ],
)
def test_keyword_score_sums_weights_for_matched_terms(
    text: str, keywords: dict[str, float], expected: float
) -> None:
    assert keyword_score(text, keywords) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# score_signal
# ---------------------------------------------------------------------------


def _signal(
    title: str = "t",
    summary: str | None = None,
    age_hours: float = 0.0,
    engagement: float = 0.0,
    id: int | None = None,
) -> RankableSignal:
    return RankableSignal(
        title=title,
        summary=summary,
        published_at=NOW - timedelta(hours=age_hours),
        engagement=engagement,
        id=id,
    )


def test_score_signal_combines_components_with_weights() -> None:
    weights = RankingWeights(
        recency=2.0,
        engagement=3.0,
        keyword=0.5,
        half_life_hours=24.0,
        engagement_saturation=100.0,
    )
    item = _signal(
        title="Rust ships new release",
        summary="Faster compiler and better diagnostics.",
        age_hours=24.0,  # recency = 0.5
        engagement=100.0,  # engagement = 1.0
    )
    keywords = {"rust": 4.0}  # keyword = 4.0

    got = score_signal(item, now=NOW, weights=weights, keywords=keywords)
    expected = 2.0 * 0.5 + 3.0 * 1.0 + 0.5 * 4.0
    assert got == pytest.approx(expected)


def test_score_signal_defaults_have_no_keyword_contribution() -> None:
    item = _signal(title="Nothing to see", age_hours=0.0, engagement=0.0)
    # Fresh + zero engagement + no keywords = only the recency component.
    assert score_signal(item, now=NOW) == pytest.approx(1.0)


def test_score_signal_matches_keywords_in_summary_too() -> None:
    item = _signal(title="Weekly update", summary="Now with Rust support")
    got = score_signal(
        item,
        now=NOW,
        weights=RankingWeights(recency=0.0, engagement=0.0, keyword=1.0),
        keywords={"rust": 3.0},
    )
    assert got == pytest.approx(3.0)


def test_score_signal_handles_missing_summary_and_timestamp() -> None:
    item = RankableSignal(title="No metadata", summary=None, published_at=None)
    got = score_signal(item, now=NOW, keywords={"anything": 1.0})
    # Recency 0, engagement 0, no keyword match -> 0.
    assert got == 0.0


# ---------------------------------------------------------------------------
# rank_signals
# ---------------------------------------------------------------------------


def test_rank_signals_orders_by_descending_composite_score() -> None:
    fresh_low = _signal(title="fresh", age_hours=0.0, engagement=0.0, id=1)
    stale_high = _signal(title="stale", age_hours=96.0, engagement=100.0, id=2)
    mid = _signal(title="mid", age_hours=24.0, engagement=50.0, id=3)

    ranked = rank_signals([fresh_low, stale_high, mid], now=NOW)

    assert [r.signal.id for r in ranked] == [mid.id, stale_high.id, fresh_low.id]
    assert all(isinstance(r, RankedSignal) for r in ranked)
    # Components are surfaced so callers can debug the ordering.
    assert set(ranked[0].components) == {"recency", "engagement", "keyword"}


def test_rank_signals_keyword_weights_can_flip_order() -> None:
    generic = _signal(title="Generic news", age_hours=0.0, engagement=10.0, id=1)
    niche = _signal(title="Deep dive on Rust", age_hours=12.0, engagement=0.0, id=2)

    # Without keywords, ``generic`` outranks ``niche``.
    baseline = rank_signals([generic, niche], now=NOW)
    assert [r.signal.id for r in baseline] == [generic.id, niche.id]

    # Boosting the ``rust`` keyword flips the order.
    boosted = rank_signals(
        [generic, niche],
        now=NOW,
        keywords={"rust": 5.0},
    )
    assert [r.signal.id for r in boosted] == [niche.id, generic.id]


def test_rank_signals_is_stable_on_ties() -> None:
    a = _signal(title="a", age_hours=0.0, engagement=0.0, id=1)
    b = _signal(title="b", age_hours=0.0, engagement=0.0, id=2)
    c = _signal(title="c", age_hours=0.0, engagement=0.0, id=3)

    ranked = rank_signals([a, b, c], now=NOW)

    # All three have identical scores; input order is preserved.
    assert [r.signal.id for r in ranked] == [1, 2, 3]
    assert all(r.score == ranked[0].score for r in ranked)


def test_rank_signals_on_empty_input_returns_empty_list() -> None:
    assert rank_signals([], now=NOW) == []
