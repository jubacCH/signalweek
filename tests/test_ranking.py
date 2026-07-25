"""Tests for the deterministic ranking module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from signalweek.ranking import (
    DEFAULT_HALF_LIFE_HOURS,
    DEFAULT_MIX,
    DEFAULT_SOURCE_WEIGHT,
    DEFAULT_SOURCE_WEIGHTS,
    RankingItem,
    ScoredItem,
    cluster_score,
    cluster_sizes,
    extract_keywords,
    rank_items,
    recency_score,
    source_score,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _item(
    url: str,
    title: str,
    *,
    source: str | None = None,
    hours_ago: float | None = None,
) -> RankingItem:
    published_at = NOW - timedelta(hours=hours_ago) if hours_ago is not None else None
    return RankingItem(url=url, title=title, source=source, published_at=published_at)


class TestRecencyScore:
    def test_now_returns_one(self) -> None:
        assert recency_score(NOW, now=NOW) == 1.0

    def test_half_life_returns_half(self) -> None:
        published = NOW - timedelta(hours=DEFAULT_HALF_LIFE_HOURS)
        assert recency_score(published, now=NOW) == pytest.approx(0.5)

    def test_double_half_life_returns_quarter(self) -> None:
        published = NOW - timedelta(hours=DEFAULT_HALF_LIFE_HOURS * 2)
        assert recency_score(published, now=NOW) == pytest.approx(0.25)

    def test_custom_half_life(self) -> None:
        published = NOW - timedelta(hours=24)
        assert recency_score(published, now=NOW, half_life_hours=24) == pytest.approx(0.5)

    def test_missing_timestamp_scores_zero(self) -> None:
        assert recency_score(None, now=NOW) == 0.0

    def test_future_item_scores_zero(self) -> None:
        published = NOW + timedelta(hours=1)
        assert recency_score(published, now=NOW) == 0.0

    def test_non_positive_half_life_scores_zero(self) -> None:
        assert recency_score(NOW, now=NOW, half_life_hours=0) == 0.0
        assert recency_score(NOW, now=NOW, half_life_hours=-5) == 0.0

    def test_is_pure(self) -> None:
        published = NOW - timedelta(hours=12)
        first = recency_score(published, now=NOW)
        second = recency_score(published, now=NOW)
        assert first == second


class TestSourceScore:
    def test_known_source(self) -> None:
        assert source_score("Hacker News") == DEFAULT_SOURCE_WEIGHTS["Hacker News"]

    def test_unknown_source_uses_default(self) -> None:
        assert source_score("nobody-knows") == DEFAULT_SOURCE_WEIGHT

    def test_none_source_uses_default(self) -> None:
        assert source_score(None) == DEFAULT_SOURCE_WEIGHT

    def test_custom_weights_and_default(self) -> None:
        weights = {"custom": 0.42}
        assert source_score("custom", weights, default=0.1) == 0.42
        assert source_score("other", weights, default=0.1) == 0.1


class TestExtractKeywords:
    def test_lowercases_and_dedupes(self) -> None:
        result = extract_keywords("LLMs are powerful, LLMs help")
        assert result == frozenset({"llms", "powerful", "help"})

    def test_strips_stopwords(self) -> None:
        # "the", "and", "for" are stopwords.
        assert extract_keywords("The Rust and Go for backend") == frozenset({"rust", "backend"})

    def test_ignores_short_and_non_word_tokens(self) -> None:
        # "a", "an", "42" (starts with a digit) all rejected;
        # "c++" kept via the `+` allowance; "co" too short.
        assert extract_keywords("a an co c++ 42 golang") == frozenset({"c++", "golang"})

    def test_empty_string(self) -> None:
        assert extract_keywords("") == frozenset()


class TestClusterSizes:
    def test_no_shared_keywords_all_size_one(self) -> None:
        items = [
            _item("https://a", "Kubernetes networking"),
            _item("https://b", "Distributed databases"),
            _item("https://c", "Rust ownership"),
        ]
        assert cluster_sizes(items) == [1, 1, 1]

    def test_shared_keyword_expands_cluster(self) -> None:
        items = [
            _item("https://a", "Rust ownership model"),
            _item("https://b", "Rust async runtimes"),
            _item("https://c", "Kubernetes networking"),
        ]
        assert cluster_sizes(items) == [2, 2, 1]

    def test_transitive_via_second_keyword_does_not_merge(self) -> None:
        # First and third do not share a keyword directly; the ranker
        # counts direct co-occurrence only.
        items = [
            _item("https://a", "Rust ownership"),
            _item("https://b", "Rust asynchronous futures"),
            _item("https://c", "Asynchronous Python futures"),
        ]
        # a: shares "rust" with b -> {a, b} size 2
        # b: shares "rust" with a and ("asynchronous","futures") with c
        #    -> {a, b, c} size 3
        # c: shares "asynchronous","futures" with b -> {b, c} size 2
        assert cluster_sizes(items) == [2, 3, 2]

    def test_titleless_items_cluster_to_self(self) -> None:
        items = [_item("https://a", "a an the")]
        assert cluster_sizes(items) == [1]

    def test_empty_input(self) -> None:
        assert cluster_sizes([]) == []


class TestClusterScore:
    def test_zero_or_one_total_gives_zero(self) -> None:
        assert cluster_score(1, total=0) == 0.0
        assert cluster_score(1, total=1) == 0.0

    def test_min_and_max_bounds(self) -> None:
        assert cluster_score(1, total=5) == 0.0
        assert cluster_score(5, total=5) == 1.0

    def test_intermediate(self) -> None:
        # size 3 in a pool of 5 -> (3-1)/(5-1) = 0.5
        assert cluster_score(3, total=5) == pytest.approx(0.5)

    def test_clamped_when_size_out_of_range(self) -> None:
        assert cluster_score(0, total=3) == 0.0
        assert cluster_score(99, total=3) == 1.0


class TestRankItems:
    def test_empty_input_returns_empty_list(self) -> None:
        assert rank_items([], now=NOW) == []

    def test_deterministic_across_calls(self) -> None:
        items = [
            _item("https://a", "Rust systems programming", source="Hacker News", hours_ago=6),
            _item("https://b", "Rust async runtimes", source="GitHub Trending", hours_ago=12),
            _item("https://c", "Kubernetes networking", source="Example Blog", hours_ago=200),
        ]
        first = rank_items(items, now=NOW)
        second = rank_items(items, now=NOW)
        assert first == second
        assert [s.item.url for s in first] == [s.item.url for s in second]

    def test_ties_break_on_url_ascending(self) -> None:
        # Two items with identical everything except URL: score is the same,
        # so URL breaks the tie.
        items = [
            _item("https://z", "Same headline", source="Hacker News", hours_ago=1),
            _item("https://a", "Same headline", source="Hacker News", hours_ago=1),
        ]
        ranked = rank_items(items, now=NOW)
        assert [s.item.url for s in ranked] == ["https://a", "https://z"]
        assert ranked[0].score == ranked[1].score

    def test_scores_ordered_desc(self) -> None:
        items = [
            _item("https://old", "Kubernetes networking", source=None, hours_ago=500),
            _item("https://fresh", "Rust systems programming", source="Hacker News", hours_ago=1),
        ]
        ranked = rank_items(items, now=NOW)
        assert ranked[0].item.url == "https://fresh"
        assert ranked[0].score > ranked[1].score

    def test_composite_matches_manual_calculation(self) -> None:
        # Fixed input, hand-computed expected composite score.
        items = [
            _item("https://one", "Rust ownership", source="Hacker News", hours_ago=48),
            _item("https://two", "Rust async", source="GitHub Trending", hours_ago=24),
            _item("https://three", "Kubernetes networking", source=None, hours_ago=0),
        ]
        ranked = rank_items(items, now=NOW)
        by_url = {entry.item.url: entry for entry in ranked}

        # Cluster sizes: one & two share "rust" -> 2, three -> 1. Total = 3.
        # cluster_score = (size-1)/(total-1) so 0.5, 0.5, 0.0.
        assert by_url["https://one"].cluster_size == 2
        assert by_url["https://two"].cluster_size == 2
        assert by_url["https://three"].cluster_size == 1
        assert by_url["https://one"].cluster == pytest.approx(0.5)
        assert by_url["https://three"].cluster == 0.0

        # Recency: 48h half-life so one=0.5, two=0.5**(24/48)=~0.7071, three=1.0
        assert by_url["https://one"].recency == pytest.approx(0.5)
        assert by_url["https://two"].recency == pytest.approx(0.5 ** 0.5)
        assert by_url["https://three"].recency == 1.0

        # Source weights: HN=0.9, GH=0.8, None-> default 0.5.
        assert by_url["https://one"].source_weight == 0.9
        assert by_url["https://two"].source_weight == 0.8
        assert by_url["https://three"].source_weight == 0.5

        w_r, w_s, w_c = DEFAULT_MIX
        expected_one = w_r * 0.5 + w_s * 0.9 + w_c * 0.5
        assert by_url["https://one"].score == pytest.approx(expected_one)

    def test_custom_mix_shifts_ordering(self) -> None:
        items = [
            _item("https://fresh-obscure", "Alpha beta", source="nobody", hours_ago=1),
            _item("https://stale-strong", "Gamma delta", source="Hacker News", hours_ago=500),
        ]
        # Recency-only mix picks the fresh item.
        recency_only = rank_items(items, now=NOW, mix=(1.0, 0.0, 0.0))
        assert recency_only[0].item.url == "https://fresh-obscure"
        # Source-only mix picks the well-sourced but stale item.
        source_only = rank_items(items, now=NOW, mix=(0.0, 1.0, 0.0))
        assert source_only[0].item.url == "https://stale-strong"

    def test_returns_scored_item_instances(self) -> None:
        items = [_item("https://a", "Something", source="Hacker News", hours_ago=1)]
        ranked = rank_items(items, now=NOW)
        assert isinstance(ranked[0], ScoredItem)
        assert ranked[0].item.url == "https://a"

    def test_input_iterable_not_mutated(self) -> None:
        items = [
            _item("https://a", "Rust", source="Hacker News", hours_ago=1),
            _item("https://b", "Go", source="Hacker News", hours_ago=1),
        ]
        snapshot = list(items)
        rank_items(items, now=NOW)
        assert items == snapshot
