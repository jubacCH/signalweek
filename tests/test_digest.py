"""Tests for the weekly digest assembler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db import Issue, SignalItem
from signalweek.digest import (
    assemble_digest,
    group_by_cluster,
    iso_week_bounds,
    iso_year_week,
    issue_number_for,
    render_markdown,
)
from signalweek.ranking import RankingItem, rank_items

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)  # Saturday, ISO 2026-W30
WEEK_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)  # Monday
WEEK_END = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)  # Next Monday


def _seed(
    url: str,
    title: str,
    *,
    source: str | None = None,
    published_at: datetime | None = None,
) -> SignalItem:
    return SignalItem(url=url, title=title, source=source, published_at=published_at)


class TestIsoWeek:
    def test_bounds_are_monday_to_next_monday_utc(self) -> None:
        assert iso_week_bounds(NOW) == (WEEK_START, WEEK_END)

    def test_bounds_on_monday_midnight_returns_same_week(self) -> None:
        # Monday 00:00 UTC belongs to its own ISO week.
        monday = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        assert iso_week_bounds(monday) == (WEEK_START, WEEK_END)

    def test_bounds_on_sunday_end_of_week(self) -> None:
        sunday = datetime(2026, 7, 26, 23, 59, tzinfo=UTC)
        assert iso_week_bounds(sunday) == (WEEK_START, WEEK_END)

    def test_bounds_normalize_naive_input_as_utc(self) -> None:
        naive = datetime(2026, 7, 25, 12, 0)
        assert iso_week_bounds(naive) == (WEEK_START, WEEK_END)

    def test_bounds_convert_non_utc_tz(self) -> None:
        # Same instant expressed in a +02:00 offset should give same bounds.
        from datetime import timezone
        aware = datetime(2026, 7, 25, 14, 0, tzinfo=timezone(timedelta(hours=2)))
        assert iso_week_bounds(aware) == (WEEK_START, WEEK_END)

    def test_year_and_number(self) -> None:
        assert iso_year_week(NOW) == (2026, 30)
        assert issue_number_for(NOW) == 202630


class TestGroupByCluster:
    def test_empty_input(self) -> None:
        assert group_by_cluster([]) == []

    def test_singleton_uses_top_keyword_as_label(self) -> None:
        items = [RankingItem(url="https://a", title="Kubernetes networking")]
        scored = rank_items(items, now=NOW)
        clusters = group_by_cluster(scored)
        assert len(clusters) == 1
        # "kubernetes" and "networking" appear once each; alphabetical wins.
        assert clusters[0].label == "kubernetes"
        assert [e.item.url for e in clusters[0].entries] == ["https://a"]

    def test_shared_keyword_merges_into_one_cluster(self) -> None:
        items = [
            RankingItem(url="https://a", title="Rust ownership model"),
            RankingItem(url="https://b", title="Rust async runtimes"),
            RankingItem(url="https://c", title="Kubernetes networking"),
        ]
        scored = rank_items(items, now=NOW)
        clusters = group_by_cluster(scored)
        # Two clusters: {a, b} under "rust", {c} under "kubernetes".
        assert len(clusters) == 2
        by_label = {c.label: c for c in clusters}
        assert set(by_label) == {"rust", "kubernetes"}
        assert {e.item.url for e in by_label["rust"].entries} == {
            "https://a",
            "https://b",
        }
        assert {e.item.url for e in by_label["kubernetes"].entries} == {"https://c"}

    def test_transitive_via_bridge_item_merges_all_three(self) -> None:
        # a-b share "rust"; b-c share "async"; a and c do not share a keyword
        # directly, but union-find over co-occurrence puts all three together.
        items = [
            RankingItem(url="https://a", title="Rust ownership"),
            RankingItem(url="https://b", title="Rust async futures"),
            RankingItem(url="https://c", title="Async Python futures"),
        ]
        scored = rank_items(items, now=NOW)
        clusters = group_by_cluster(scored)
        assert len(clusters) == 1
        assert {e.item.url for e in clusters[0].entries} == {
            "https://a",
            "https://b",
            "https://c",
        }

    def test_clusters_sort_by_max_score_desc_then_label(self) -> None:
        # Make cluster "rust" have the higher max score by giving one member
        # a stronger source + fresh timestamp.
        items = [
            RankingItem(
                url="https://rust-hot",
                title="Rust performance",
                source="Hacker News",
                published_at=NOW - timedelta(hours=1),
            ),
            RankingItem(
                url="https://rust-cold",
                title="Rust ergonomics",
                source=None,
                published_at=NOW - timedelta(hours=200),
            ),
            RankingItem(
                url="https://kube",
                title="Kubernetes networking",
                source=None,
                published_at=NOW - timedelta(hours=200),
            ),
        ]
        scored = rank_items(items, now=NOW)
        clusters = group_by_cluster(scored)
        assert [c.label for c in clusters] == ["rust", "kubernetes"]

    def test_deterministic_across_calls(self) -> None:
        items = [
            RankingItem(url="https://a", title="Rust", source="Hacker News"),
            RankingItem(url="https://b", title="Rust", source="Hacker News"),
        ]
        scored = rank_items(items, now=NOW)
        assert group_by_cluster(scored) == group_by_cluster(scored)


class TestRenderMarkdown:
    def test_header_intro_and_cluster_sections(self) -> None:
        items = [
            RankingItem(
                url="https://one",
                title="Rust ownership",
                source="Hacker News",
                published_at=NOW - timedelta(hours=1),
            ),
            RankingItem(
                url="https://two",
                title="Rust async",
                source="GitHub Trending",
                published_at=NOW - timedelta(hours=2),
            ),
            RankingItem(
                url="https://three",
                title="Kubernetes networking",
                source=None,
                published_at=NOW - timedelta(hours=3),
            ),
        ]
        scored = rank_items(items, now=NOW)
        clusters = group_by_cluster(scored)
        body = render_markdown(
            clusters,
            iso_year=2026,
            iso_week=30,
            window_start=WEEK_START,
            window_end=WEEK_END,
        )
        assert body.startswith("# SignalWeek 2026-W30\n")
        assert "Curated links from 2026-07-20 to 2026-07-26." in body
        assert "## Rust" in body
        assert "## Kubernetes" in body
        assert "[Rust ownership](https://one) — Hacker News" in body
        assert "[Rust async](https://two) — GitHub Trending" in body
        # No source suffix when source is missing.
        assert "[Kubernetes networking](https://three)\n" in body
        assert body.endswith("\n")

    def test_empty_clusters_emit_placeholder(self) -> None:
        body = render_markdown(
            [],
            iso_year=2026,
            iso_week=30,
            window_start=WEEK_START,
            window_end=WEEK_END,
        )
        assert "_No signals this week._" in body


class TestAssembleDigest:
    async def test_persists_issue_and_attaches_top_items(
        self, session: AsyncSession
    ) -> None:
        session.add_all(
            [
                _seed(
                    "https://a",
                    "Rust ownership",
                    source="Hacker News",
                    published_at=NOW - timedelta(hours=2),
                ),
                _seed(
                    "https://b",
                    "Rust async runtimes",
                    source="GitHub Trending",
                    published_at=NOW - timedelta(hours=4),
                ),
                _seed(
                    "https://c",
                    "Kubernetes networking",
                    source="Example Blog",
                    published_at=NOW - timedelta(hours=6),
                ),
            ]
        )
        await session.commit()

        issue = await assemble_digest(session, now=NOW)

        assert issue.id is not None
        assert issue.number == 202630
        assert issue.title == "SignalWeek 2026-W30"
        assert issue.published_at == WEEK_START
        assert issue.body_markdown.startswith("# SignalWeek 2026-W30\n")
        assert "## Rust" in issue.body_markdown
        assert "## Kubernetes" in issue.body_markdown
        assert {item.url for item in issue.items} == {
            "https://a",
            "https://b",
            "https://c",
        }

        # Round-trip via a fresh query to confirm persistence.
        fetched = (
            await session.execute(select(Issue).where(Issue.number == 202630))
        ).scalar_one()
        assert fetched.body_markdown == issue.body_markdown
        assert len(fetched.items) == 3

    async def test_excludes_items_outside_the_week(
        self, session: AsyncSession
    ) -> None:
        in_week = _seed(
            "https://in",
            "Inside the window",
            source="Hacker News",
            published_at=NOW - timedelta(hours=1),
        )
        before = _seed(
            "https://before",
            "Too old",
            source="Hacker News",
            published_at=WEEK_START - timedelta(hours=1),
        )
        after = _seed(
            "https://after",
            "Too new",
            source="Hacker News",
            published_at=WEEK_END + timedelta(hours=1),
        )
        missing_ts = _seed(
            "https://noon",
            "No timestamp",
            source="Hacker News",
            published_at=None,
        )
        session.add_all([in_week, before, after, missing_ts])
        await session.commit()

        issue = await assemble_digest(session, now=NOW)

        urls = {item.url for item in issue.items}
        assert urls == {"https://in"}

    async def test_top_n_limits_items_and_orders_by_score(
        self, session: AsyncSession
    ) -> None:
        # Two "hot" (fresh + strong source) plus a stale one.
        session.add_all(
            [
                _seed(
                    "https://hot-a",
                    "Rust fast",
                    source="Hacker News",
                    published_at=NOW - timedelta(hours=1),
                ),
                _seed(
                    "https://hot-b",
                    "Rust safe",
                    source="Hacker News",
                    published_at=NOW - timedelta(hours=2),
                ),
                _seed(
                    "https://stale",
                    "Kubernetes ancient",
                    source=None,
                    published_at=NOW - timedelta(hours=100),
                ),
            ]
        )
        await session.commit()

        issue = await assemble_digest(session, now=NOW, top_n=2)

        assert len(issue.items) == 2
        assert {i.url for i in issue.items} == {"https://hot-a", "https://hot-b"}

    async def test_empty_week_still_persists_a_placeholder_issue(
        self, session: AsyncSession
    ) -> None:
        # Only items outside the window.
        session.add(
            _seed(
                "https://old",
                "Ancient news",
                source="Hacker News",
                published_at=WEEK_START - timedelta(days=30),
            )
        )
        await session.commit()

        issue = await assemble_digest(session, now=NOW)

        assert issue.id is not None
        assert issue.items == []
        assert "_No signals this week._" in issue.body_markdown

    async def test_rejects_non_positive_top_n(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError):
            await assemble_digest(session, now=NOW, top_n=0)

    async def test_second_call_same_week_conflicts_on_issue_number(
        self, session: AsyncSession
    ) -> None:
        session.add(
            _seed(
                "https://a",
                "Rust",
                source="Hacker News",
                published_at=NOW - timedelta(hours=1),
            )
        )
        await session.commit()

        await assemble_digest(session, now=NOW)
        # The Issue.number column is unique; re-running the same week fails.
        with pytest.raises(IntegrityError):
            await assemble_digest(session, now=NOW)
        await session.rollback()
