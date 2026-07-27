"""Tests for :mod:`signalweek.ingest.discover`.

The discovery module is data-driven: it walks the ``items`` table (joined
to ``issues.week_of``) and rewrites ``source_candidates``, then promotes
any candidate above the configured citation/week thresholds into the
``sources`` registry with a full audit trail.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest.discover import (
    DEFAULT_MIN_CITE_COUNT,
    DEFAULT_MIN_DISTINCT_WEEKS,
    discover_and_promote,
    mine_cited_domains,
    promote_candidates,
)
from signalweek.sources import (
    issues_table,
    items_table,
    source_candidates_table,
    sources_metadata,
    sources_table,
)


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _insert_source(
    conn: Connection,
    *,
    url: str,
    category_hint: str | None = "industry_moves",
    kind: str = "rss",
    active: bool = True,
) -> int:
    result = conn.execute(
        sources_table.insert()
        .values(url=url, kind=kind, category_hint=category_hint, active=active)
        .returning(sources_table.c.id)
    )
    return int(result.scalar_one())


def _insert_cluster(conn: Connection, *, primary_url: str, headline: str = "hl") -> int:
    from signalweek.sources import clusters_table

    result = conn.execute(
        clusters_table.insert()
        .values(
            primary_url=primary_url,
            category="industry_moves",
            canonical_headline=headline,
        )
        .returning(clusters_table.c.id)
    )
    return int(result.scalar_one())


def _insert_issue(conn: Connection, *, week_of: date, status: str = "published") -> int:
    result = conn.execute(
        issues_table.insert()
        .values(
            week_of=week_of,
            status=status,
            published_at=datetime(2026, 1, 1, tzinfo=UTC) if status == "published" else None,
        )
        .returning(issues_table.c.id)
    )
    return int(result.scalar_one())


def _insert_item(
    conn: Connection,
    *,
    issue_id: int,
    cluster_id: int,
    position: int,
    primary_url: str,
    extras: list[str] | None = None,
    headline: str = "hl",
) -> int:
    result = conn.execute(
        items_table.insert()
        .values(
            issue_id=issue_id,
            cluster_id=cluster_id,
            category="industry_moves",
            position=position,
            headline=headline,
            summary=headline,
            primary_url=primary_url,
            extra_source_urls=extras or [],
        )
        .returning(items_table.c.id)
    )
    return int(result.scalar_one())


def _candidate_by_domain(conn: Connection, domain: str) -> dict[str, object] | None:
    row = conn.execute(
        select(source_candidates_table).where(source_candidates_table.c.domain == domain)
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# mine_cited_domains
# ---------------------------------------------------------------------------


class TestMineCitedDomains:
    def test_counts_primary_and_extra_source_domains(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            issue_id = _insert_issue(conn, week_of=date(2026, 7, 6))
            cluster_id = _insert_cluster(conn, primary_url="https://newco.io/a")
            _insert_item(
                conn,
                issue_id=issue_id,
                cluster_id=cluster_id,
                position=1,
                primary_url="https://newco.io/a",
                extras=[
                    "https://mirror.example/story",
                    "https://third.example/repost",
                ],
            )

            result = mine_cited_domains(conn)
            rows = conn.execute(
                select(source_candidates_table).order_by(source_candidates_table.c.domain)
            ).all()

        assert result.inserted == 3
        assert [r.domain for r in rows] == [
            "mirror.example",
            "newco.io",
            "third.example",
        ]
        assert all(int(r.cite_count) == 1 for r in rows)
        assert all(int(r.distinct_weeks_count) == 1 for r in rows)

    def test_deduplicates_domain_within_a_single_item(self, curated_engine: Engine) -> None:
        # The same domain on the primary URL and on an extra URL of the same
        # item should count as a single citation, not two.
        with curated_engine.begin() as conn:
            issue_id = _insert_issue(conn, week_of=date(2026, 7, 6))
            cluster_id = _insert_cluster(conn, primary_url="https://newco.io/a")
            _insert_item(
                conn,
                issue_id=issue_id,
                cluster_id=cluster_id,
                position=1,
                primary_url="https://newco.io/a",
                extras=["https://newco.io/b", "https://other.example/x"],
            )

            mine_cited_domains(conn)
            newco = _candidate_by_domain(conn, "newco.io")

        assert newco is not None
        assert int(newco["cite_count"]) == 1

    def test_counts_distinct_weeks(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            i1 = _insert_issue(conn, week_of=date(2026, 6, 29))
            i2 = _insert_issue(conn, week_of=date(2026, 7, 6))
            i3 = _insert_issue(conn, week_of=date(2026, 7, 13))
            c = _insert_cluster(conn, primary_url="https://newco.io/a")
            c2 = _insert_cluster(conn, primary_url="https://newco.io/b")
            c3 = _insert_cluster(conn, primary_url="https://newco.io/c")
            _insert_item(
                conn, issue_id=i1, cluster_id=c, position=1, primary_url="https://newco.io/a"
            )
            _insert_item(
                conn, issue_id=i2, cluster_id=c2, position=1, primary_url="https://newco.io/b"
            )
            _insert_item(
                conn, issue_id=i3, cluster_id=c3, position=1, primary_url="https://newco.io/c"
            )

            mine_cited_domains(conn)
            row = _candidate_by_domain(conn, "newco.io")

        assert row is not None
        assert int(row["cite_count"]) == 3
        assert int(row["distinct_weeks_count"]) == 3
        assert row["first_seen_week"] == date(2026, 6, 29)
        assert row["last_seen_week"] == date(2026, 7, 13)

    def test_strips_www_and_lowercases(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            issue_id = _insert_issue(conn, week_of=date(2026, 7, 6))
            c1 = _insert_cluster(conn, primary_url="https://WWW.NewCo.io/a")
            c2 = _insert_cluster(conn, primary_url="https://newco.io/b")
            _insert_item(
                conn,
                issue_id=issue_id,
                cluster_id=c1,
                position=1,
                primary_url="https://WWW.NewCo.io/a",
            )
            _insert_item(
                conn,
                issue_id=issue_id,
                cluster_id=c2,
                position=2,
                primary_url="https://newco.io/b",
            )

            mine_cited_domains(conn)
            rows = conn.execute(select(source_candidates_table)).all()

        assert len(rows) == 1
        assert rows[0].domain == "newco.io"
        assert int(rows[0].cite_count) == 2

    def test_is_idempotent_when_rerun_without_changes(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            issue_id = _insert_issue(conn, week_of=date(2026, 7, 6))
            cluster_id = _insert_cluster(conn, primary_url="https://newco.io/a")
            _insert_item(
                conn,
                issue_id=issue_id,
                cluster_id=cluster_id,
                position=1,
                primary_url="https://newco.io/a",
            )

            first = mine_cited_domains(conn)
        with curated_engine.begin() as conn:
            second = mine_cited_domains(conn)
            row = _candidate_by_domain(conn, "newco.io")

        assert first.inserted == 1
        assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1)
        assert row is not None
        assert int(row["cite_count"]) == 1

    def test_preserves_promotion_state_when_recounting(self, curated_engine: Engine) -> None:
        # A domain that has already been promoted must keep its ``promoted``
        # / ``promoted_at`` / ``promoted_source_id`` fields when we re-mine
        # after new citations arrive.
        with curated_engine.begin() as conn:
            i1 = _insert_issue(conn, week_of=date(2026, 6, 29))
            i2 = _insert_issue(conn, week_of=date(2026, 7, 6))
            i3 = _insert_issue(conn, week_of=date(2026, 7, 13))
            c1 = _insert_cluster(conn, primary_url="https://newco.io/a")
            c2 = _insert_cluster(conn, primary_url="https://newco.io/b")
            c3 = _insert_cluster(conn, primary_url="https://newco.io/c")
            _insert_item(
                conn, issue_id=i1, cluster_id=c1, position=1, primary_url="https://newco.io/a"
            )
            _insert_item(
                conn, issue_id=i2, cluster_id=c2, position=1, primary_url="https://newco.io/b"
            )
            _insert_item(
                conn, issue_id=i3, cluster_id=c3, position=1, primary_url="https://newco.io/c"
            )

            mine_cited_domains(conn)
            promote_candidates(
                conn,
                now=datetime(2026, 7, 20, tzinfo=UTC),
                min_cite_count=3,
                min_distinct_weeks=2,
            )

            # Add another citation in a new week and re-mine.
            i4 = _insert_issue(conn, week_of=date(2026, 7, 20))
            c4 = _insert_cluster(conn, primary_url="https://newco.io/d")
            _insert_item(
                conn, issue_id=i4, cluster_id=c4, position=1, primary_url="https://newco.io/d"
            )

            mine_cited_domains(conn)
            row = _candidate_by_domain(conn, "newco.io")

        assert row is not None
        assert bool(row["promoted"]) is True
        assert row["promoted_at"] is not None
        assert row["promoted_source_id"] is not None
        assert int(row["cite_count"]) == 4
        assert int(row["distinct_weeks_count"]) == 4

    def test_removes_stale_candidate_rows(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            issue_id = _insert_issue(conn, week_of=date(2026, 7, 6))
            c = _insert_cluster(conn, primary_url="https://newco.io/a")
            item_id = _insert_item(
                conn, issue_id=issue_id, cluster_id=c, position=1, primary_url="https://newco.io/a"
            )
            mine_cited_domains(conn)
            # Wipe the item — the candidate row should disappear on re-mine.
            conn.execute(items_table.delete().where(items_table.c.id == item_id))

            mine_cited_domains(conn)
            rows = conn.execute(select(source_candidates_table)).all()

        assert rows == []

    def test_ignores_items_from_held_and_draft_issues_only_via_week_of(
        self, curated_engine: Engine
    ) -> None:
        # Non-published issues are still real issues; their items count too.
        # This confirms mining does not silently filter on ``issues.status``.
        with curated_engine.begin() as conn:
            held = _insert_issue(conn, week_of=date(2026, 7, 6), status="held")
            c = _insert_cluster(conn, primary_url="https://newco.io/a")
            _insert_item(
                conn, issue_id=held, cluster_id=c, position=1, primary_url="https://newco.io/a"
            )

            mine_cited_domains(conn)
            row = _candidate_by_domain(conn, "newco.io")

        assert row is not None
        assert int(row["cite_count"]) == 1


# ---------------------------------------------------------------------------
# promote_candidates
# ---------------------------------------------------------------------------


def _seed_candidate(
    conn: Connection,
    *,
    domain: str,
    first_seen_week: date,
    last_seen_week: date | None = None,
    cite_count: int,
    distinct_weeks_count: int,
) -> int:
    result = conn.execute(
        source_candidates_table.insert()
        .values(
            domain=domain,
            first_seen_week=first_seen_week,
            last_seen_week=last_seen_week or first_seen_week,
            cite_count=cite_count,
            distinct_weeks_count=distinct_weeks_count,
            promoted=False,
            promoted_at=None,
            promoted_source_id=None,
        )
        .returning(source_candidates_table.c.id)
    )
    return int(result.scalar_one())


class TestPromoteCandidates:
    def test_promotes_a_candidate_that_clears_the_threshold(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            candidate_id = _seed_candidate(
                conn,
                domain="newco.io",
                first_seen_week=date(2026, 6, 29),
                last_seen_week=date(2026, 7, 13),
                cite_count=4,
                distinct_weeks_count=3,
            )
            now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
            result = promote_candidates(conn, now=now, min_cite_count=3, min_distinct_weeks=2)

            source_row = conn.execute(
                select(sources_table).where(sources_table.c.discovered.is_(True))
            ).one()
            candidate = _candidate_by_domain(conn, "newco.io")

        assert result.promoted == 1
        promotion = result.promotions[0]
        assert promotion.domain == "newco.io"
        assert promotion.cite_count == 4
        assert promotion.first_seen_week == date(2026, 6, 29)

        assert source_row.url == "https://newco.io/"
        assert source_row.kind == "rss"
        assert bool(source_row.active) is True
        assert bool(source_row.discovered) is True
        assert source_row.discovered_first_seen_week == date(2026, 6, 29)
        assert int(source_row.discovered_cite_count) == 4

        assert candidate is not None
        assert bool(candidate["promoted"]) is True
        assert candidate["promoted_at"] is not None
        assert int(candidate["promoted_source_id"]) == int(source_row.id)
        assert int(candidate["id"]) == candidate_id

    def test_skips_below_cite_threshold(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            _seed_candidate(
                conn,
                domain="tiny.example",
                first_seen_week=date(2026, 6, 29),
                last_seen_week=date(2026, 7, 6),
                cite_count=2,
                distinct_weeks_count=2,
            )
            result = promote_candidates(
                conn,
                now=datetime(2026, 7, 20, tzinfo=UTC),
                min_cite_count=3,
                min_distinct_weeks=2,
            )
            sources_present = conn.execute(select(sources_table)).all()

        assert result.promoted == 0
        assert result.skipped_below_threshold == 1
        assert sources_present == []

    def test_skips_below_week_threshold(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            _seed_candidate(
                conn,
                domain="oneweek.example",
                first_seen_week=date(2026, 6, 29),
                last_seen_week=date(2026, 6, 29),
                cite_count=5,
                distinct_weeks_count=1,
            )
            result = promote_candidates(
                conn,
                now=datetime(2026, 7, 20, tzinfo=UTC),
                min_cite_count=3,
                min_distinct_weeks=2,
            )
            sources_present = conn.execute(select(sources_table)).all()

        assert result.promoted == 0
        assert result.skipped_below_threshold == 1
        assert sources_present == []

    def test_skips_domains_already_in_sources(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            _insert_source(conn, url="https://openai.com/blog/rss.xml")
            _seed_candidate(
                conn,
                domain="openai.com",
                first_seen_week=date(2026, 6, 29),
                last_seen_week=date(2026, 7, 13),
                cite_count=8,
                distinct_weeks_count=3,
            )
            result = promote_candidates(
                conn,
                now=datetime(2026, 7, 20, tzinfo=UTC),
                min_cite_count=3,
                min_distinct_weeks=2,
            )
            row = _candidate_by_domain(conn, "openai.com")
            all_sources = conn.execute(select(sources_table)).all()

        assert result.promoted == 0
        assert result.skipped_existing_source == 1
        assert row is not None
        assert bool(row["promoted"]) is False
        assert len(all_sources) == 1

    def test_does_not_re_promote_a_promoted_candidate(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            _seed_candidate(
                conn,
                domain="newco.io",
                first_seen_week=date(2026, 6, 29),
                last_seen_week=date(2026, 7, 13),
                cite_count=4,
                distinct_weeks_count=3,
            )
            promote_candidates(
                conn,
                now=datetime(2026, 7, 20, tzinfo=UTC),
                min_cite_count=3,
                min_distinct_weeks=2,
            )
        with curated_engine.begin() as conn:
            result = promote_candidates(
                conn,
                now=datetime(2026, 7, 27, tzinfo=UTC),
                min_cite_count=3,
                min_distinct_weeks=2,
            )
            sources_rows = conn.execute(
                select(sources_table).where(sources_table.c.discovered.is_(True))
            ).all()

        assert result.promoted == 0
        # Only one discovered source ever gets inserted.
        assert len(sources_rows) == 1

    def test_thresholds_are_configurable(self, curated_engine: Engine) -> None:
        # A candidate that fails the default gate can still be promoted with
        # a looser threshold — the discovery module owns the promotion rules,
        # the numbers themselves are the operator's knob.
        with curated_engine.begin() as conn:
            _seed_candidate(
                conn,
                domain="edge.example",
                first_seen_week=date(2026, 6, 29),
                last_seen_week=date(2026, 7, 6),
                cite_count=2,
                distinct_weeks_count=2,
            )
            result = promote_candidates(
                conn,
                now=datetime(2026, 7, 20, tzinfo=UTC),
                min_cite_count=2,
                min_distinct_weeks=2,
            )
            source_row = conn.execute(
                select(sources_table).where(sources_table.c.discovered.is_(True))
            ).one()

        assert result.promoted == 1
        assert source_row.url == "https://edge.example/"
        assert int(source_row.discovered_cite_count) == 2


# ---------------------------------------------------------------------------
# End-to-end wrapper
# ---------------------------------------------------------------------------


class TestDiscoverAndPromote:
    def test_end_to_end_grows_the_registry_from_items(self, curated_engine: Engine) -> None:
        # Seed only the incumbent sources and a batch of items whose extras
        # keep pointing at a new outlet. The discovery pass should mine the
        # citations and promote the newcomer without any manual input.
        with curated_engine.begin() as conn:
            _insert_source(conn, url="https://openai.com/blog/rss.xml")

            for week_index, week in enumerate(
                (date(2026, 6, 15), date(2026, 6, 22), date(2026, 6, 29))
            ):
                issue_id = _insert_issue(conn, week_of=week)
                cluster_id = _insert_cluster(
                    conn, primary_url=f"https://openai.com/story-{week_index}"
                )
                _insert_item(
                    conn,
                    issue_id=issue_id,
                    cluster_id=cluster_id,
                    position=1,
                    primary_url=f"https://openai.com/story-{week_index}",
                    extras=["https://insightlab.io/coverage"],
                )

            now = datetime(2026, 7, 6, tzinfo=UTC)
            mining, promotion = discover_and_promote(
                conn,
                now=now,
                min_cite_count=3,
                min_distinct_weeks=2,
            )

            new_sources = conn.execute(
                select(sources_table).where(sources_table.c.discovered.is_(True))
            ).all()
            candidate = _candidate_by_domain(conn, "insightlab.io")

        assert mining.total >= 2  # openai.com + insightlab.io
        assert promotion.promoted == 1
        [new_source] = new_sources
        assert new_source.url == "https://insightlab.io/"
        assert bool(new_source.discovered) is True
        assert new_source.discovered_first_seen_week == date(2026, 6, 15)
        assert int(new_source.discovered_cite_count) == 3
        assert candidate is not None
        assert bool(candidate["promoted"]) is True
        assert int(candidate["promoted_source_id"]) == int(new_source.id)


def test_default_thresholds_are_reasonable() -> None:
    # A cheap regression check: the defaults must both be at least 2 so a
    # single mention or a single burst week can never promote a domain.
    assert DEFAULT_MIN_CITE_COUNT >= 2
    assert DEFAULT_MIN_DISTINCT_WEEKS >= 2
