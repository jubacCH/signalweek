"""Tests for :mod:`signalweek.ingest.health` — the periodic prune step.

The health module keeps the source registry autonomous: the ingest layer
maintains per-source fetch/silence counters, an inactive-source probe
keeps deactivated rows' counters fresh, and :func:`prune_sources` applies
the rules that flip ``sources.active`` on and off — logging every state
change in ``source_health_events``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest import ingest_all_active, ingest_source
from signalweek.ingest.health import (
    ACTION_ACTIVATED,
    ACTION_DEACTIVATED,
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_SILENT_WEEKS,
    REASON_FETCH_FAILURES,
    REASON_RECOVERED,
    REASON_SILENT,
    probe_inactive_sources,
    prune_sources,
    record_fetch_failure,
    record_fetch_success,
    record_items_seen,
)
from signalweek.sources import (
    source_health_events_table,
    sources_metadata,
    sources_table,
)

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
FIXED_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _insert_source(
    conn: Connection,
    *,
    url: str,
    active: bool = True,
    consecutive_fetch_failures: int = 0,
    last_fetch_ok_at: datetime | None = None,
    last_fetch_error_at: datetime | None = None,
    last_item_at: datetime | None = None,
    deactivated_at: datetime | None = None,
    deactivation_reason: str | None = None,
) -> int:
    result = conn.execute(
        sources_table.insert()
        .values(
            url=url,
            kind="rss",
            category_hint="industry_moves",
            active=active,
            consecutive_fetch_failures=consecutive_fetch_failures,
            last_fetch_ok_at=last_fetch_ok_at,
            last_fetch_error_at=last_fetch_error_at,
            last_item_at=last_item_at,
            deactivated_at=deactivated_at,
            deactivation_reason=deactivation_reason,
        )
        .returning(sources_table.c.id)
    )
    return int(result.scalar_one())


def _source_row(conn: Connection, source_id: int):
    return conn.execute(select(sources_table).where(sources_table.c.id == source_id)).one()


def _events(conn: Connection, source_id: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        select(
            source_health_events_table.c.action,
            source_health_events_table.c.reason,
            source_health_events_table.c.at,
        )
        .where(source_health_events_table.c.source_id == source_id)
        .order_by(source_health_events_table.c.id)
    ).all()
    return [(r.action, r.reason) for r in rows]


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------


class TestRecordingHelpers:
    def test_record_fetch_success_resets_failures_and_stamps_ok(
        self, curated_engine: Engine
    ) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(conn, url="https://ok.example/feed", consecutive_fetch_failures=3)

            record_fetch_success(conn, source_id=sid, now=FIXED_NOW)
            row = _source_row(conn, sid)

        assert int(row.consecutive_fetch_failures) == 0
        assert row.last_fetch_ok_at is not None

    def test_record_fetch_failure_increments_and_stamps_error(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(conn, url="https://broken.example/feed")

            record_fetch_failure(conn, source_id=sid, now=FIXED_NOW)
            record_fetch_failure(conn, source_id=sid, now=FIXED_NOW)
            row = _source_row(conn, sid)

        assert int(row.consecutive_fetch_failures) == 2
        assert row.last_fetch_error_at is not None

    def test_record_items_seen_stamps_last_item_at(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(conn, url="https://x.example/feed")

            record_items_seen(conn, source_id=sid, now=FIXED_NOW)
            row = _source_row(conn, sid)

        assert row.last_item_at is not None


# ---------------------------------------------------------------------------
# Ingest wiring: the fetch layer bumps the health counters automatically.
# ---------------------------------------------------------------------------


class TestIngestUpdatesHealth:
    def test_successful_ingest_resets_failures_and_stamps_last_item(
        self, curated_engine: Engine
    ) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://blog.example.com/feed",
                consecutive_fetch_failures=4,
            )

        with curated_engine.begin() as conn:
            ingest_source(
                conn,
                source_id=sid,
                url="https://blog.example.com/feed",
                content=_fixture("example_rss.xml"),
                now=FIXED_NOW,
            )
            row = _source_row(conn, sid)

        assert int(row.consecutive_fetch_failures) == 0
        assert row.last_fetch_ok_at is not None
        assert row.last_item_at is not None

    def test_successful_ingest_with_no_new_items_leaves_last_item_untouched(
        self, curated_engine: Engine
    ) -> None:
        content = _fixture("example_rss.xml")
        with curated_engine.begin() as conn:
            sid = _insert_source(conn, url="https://blog.example.com/feed")

        # First run inserts and stamps last_item_at at t=old.
        old = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        with curated_engine.begin() as conn:
            ingest_source(
                conn,
                source_id=sid,
                url="https://blog.example.com/feed",
                content=content,
                now=old,
            )
        # Second run finds nothing new; last_item_at must stay at ``old``.
        with curated_engine.begin() as conn:
            ingest_source(
                conn,
                source_id=sid,
                url="https://blog.example.com/feed",
                content=content,
                now=FIXED_NOW,
            )
            row = _source_row(conn, sid)

        assert row.last_item_at is not None
        # SQLite strips tz info on read; compare naive.
        assert row.last_item_at == old.replace(tzinfo=None)
        # But last_fetch_ok_at moved forward.
        assert row.last_fetch_ok_at == FIXED_NOW.replace(tzinfo=None)

    def test_ingest_all_active_records_fetch_failure_on_broken_source(
        self, curated_engine: Engine
    ) -> None:
        with curated_engine.begin() as conn:
            broken = _insert_source(conn, url="https://broken.example.com/feed")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with curated_engine.begin() as conn, _mock_client(handler) as client:
            ingest_all_active(conn, client=client, now=FIXED_NOW)
            row = _source_row(conn, broken)

        assert int(row.consecutive_fetch_failures) == 1
        assert row.last_fetch_error_at is not None
        assert row.last_fetch_ok_at is None

    def test_ingest_all_active_repeated_failures_accumulate(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            broken = _insert_source(conn, url="https://broken.example.com/feed")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with _mock_client(handler) as client:
            for _ in range(3):
                with curated_engine.begin() as conn:
                    ingest_all_active(conn, client=client, now=FIXED_NOW)
            with curated_engine.begin() as conn:
                row = _source_row(conn, broken)

        assert int(row.consecutive_fetch_failures) == 3

    def test_ingest_all_active_success_clears_prior_failures(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://blog.example.com/feed",
                consecutive_fetch_failures=4,
            )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_fixture("example_rss.xml"))

        with curated_engine.begin() as conn, _mock_client(handler) as client:
            ingest_all_active(conn, client=client, now=FIXED_NOW)
            row = _source_row(conn, sid)

        assert int(row.consecutive_fetch_failures) == 0


# ---------------------------------------------------------------------------
# probe_inactive_sources
# ---------------------------------------------------------------------------


class TestProbeInactiveSources:
    def test_probe_touches_only_inactive_rows(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            active = _insert_source(conn, url="https://active.example/feed", active=True)
            inactive = _insert_source(conn, url="https://inactive.example/feed", active=False)

        touched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            touched.append(str(request.url))
            return httpx.Response(200, content=_fixture("example_rss.xml"))

        with curated_engine.begin() as conn, _mock_client(handler) as client:
            result = probe_inactive_sources(conn, client=client, now=FIXED_NOW)

        assert result.probed == 1
        assert result.succeeded == 1
        assert result.failed == 0
        assert touched == ["https://inactive.example/feed"]
        # The active source was untouched.
        assert active != inactive  # smoke

    def test_probe_success_resets_failures_and_stamps_last_ok(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://recovering.example/feed",
                active=False,
                consecutive_fetch_failures=6,
                deactivated_at=FIXED_NOW - timedelta(days=1),
                deactivation_reason=REASON_FETCH_FAILURES,
            )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_fixture("example_rss.xml"))

        with curated_engine.begin() as conn, _mock_client(handler) as client:
            probe_inactive_sources(conn, client=client, now=FIXED_NOW)
            row = _source_row(conn, sid)

        assert int(row.consecutive_fetch_failures) == 0
        assert row.last_fetch_ok_at is not None
        # Probe does not itself flip ``active`` — that's prune's job.
        assert bool(row.active) is False

    def test_probe_failure_bumps_counter(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://still-dead.example/feed",
                active=False,
                consecutive_fetch_failures=5,
                deactivated_at=FIXED_NOW - timedelta(days=1),
                deactivation_reason=REASON_FETCH_FAILURES,
            )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with curated_engine.begin() as conn, _mock_client(handler) as client:
            result = probe_inactive_sources(conn, client=client, now=FIXED_NOW)
            row = _source_row(conn, sid)

        assert result.failed == 1
        assert int(row.consecutive_fetch_failures) == 6

    def test_probe_with_no_inactive_sources_is_a_noop(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            _insert_source(conn, url="https://active.example/feed", active=True)

        with curated_engine.begin() as conn:
            result = probe_inactive_sources(conn, now=FIXED_NOW)

        assert result.probed == 0
        assert result.succeeded == 0
        assert result.failed == 0


# ---------------------------------------------------------------------------
# prune_sources — deactivation rules
# ---------------------------------------------------------------------------


class TestPruneDeactivates:
    def test_deactivates_source_at_failure_threshold(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://dead.example/feed",
                consecutive_fetch_failures=5,
                last_fetch_ok_at=FIXED_NOW - timedelta(days=30),
                last_fetch_error_at=FIXED_NOW,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert len(result.deactivated) == 1
        event = result.deactivated[0]
        assert event.source_id == sid
        assert event.reason == REASON_FETCH_FAILURES
        assert bool(row.active) is False
        assert row.deactivation_reason == REASON_FETCH_FAILURES
        assert row.deactivated_at is not None

        with curated_engine.begin() as conn:
            assert _events(conn, sid) == [(ACTION_DEACTIVATED, REASON_FETCH_FAILURES)]

    def test_deactivates_silent_source_past_the_cutoff(self, curated_engine: Engine) -> None:
        old_ok = FIXED_NOW - timedelta(weeks=10)
        old_item = FIXED_NOW - timedelta(weeks=10)
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://silent.example/feed",
                consecutive_fetch_failures=0,
                last_fetch_ok_at=old_ok,
                last_item_at=old_item,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert [e.reason for e in result.deactivated] == [REASON_SILENT]
        assert bool(row.active) is False
        assert row.deactivation_reason == REASON_SILENT

    def test_deactivates_source_that_has_never_produced_items(self, curated_engine: Engine) -> None:
        # ``last_item_at`` is NULL but the source has been fetchable for
        # weeks. Silence rule applies.
        old_ok = FIXED_NOW - timedelta(weeks=10)
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://zombie.example/feed",
                consecutive_fetch_failures=0,
                last_fetch_ok_at=old_ok,
                last_item_at=None,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert [e.reason for e in result.deactivated] == [REASON_SILENT]
        assert bool(row.active) is False

    def test_keeps_healthy_source_active(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://healthy.example/feed",
                consecutive_fetch_failures=0,
                last_fetch_ok_at=FIXED_NOW,
                last_item_at=FIXED_NOW - timedelta(days=1),
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert result.deactivated == []
        assert result.reactivated == []
        assert bool(row.active) is True

    def test_silence_rule_ignores_fresh_sources_still_within_observation_window(
        self, curated_engine: Engine
    ) -> None:
        # A source we've only recently added — last_fetch_ok_at is within
        # the silent window — must not be pruned even if it has never
        # produced items yet.
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://fresh.example/feed",
                consecutive_fetch_failures=0,
                last_fetch_ok_at=FIXED_NOW - timedelta(days=2),
                last_item_at=None,
            )

            prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert bool(row.active) is True

    def test_fetch_failures_takes_precedence_over_silence(self, curated_engine: Engine) -> None:
        # A source that satisfies both rules gets the failure reason —
        # the more concrete signal wins.
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://both.example/feed",
                consecutive_fetch_failures=5,
                last_fetch_ok_at=FIXED_NOW - timedelta(weeks=10),
                last_item_at=None,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert result.deactivated[0].reason == REASON_FETCH_FAILURES
        assert row.deactivation_reason == REASON_FETCH_FAILURES


# ---------------------------------------------------------------------------
# prune_sources — reactivation
# ---------------------------------------------------------------------------


class TestPruneReactivates:
    def test_reactivates_a_source_that_recovered_from_fetch_failures(
        self, curated_engine: Engine
    ) -> None:
        # Deactivated for fetch_failures, then a probe brought it back
        # (consecutive_fetch_failures=0, last_fetch_ok_at fresh). Prune
        # should flip active back on.
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://back.example/feed",
                active=False,
                consecutive_fetch_failures=0,
                last_fetch_ok_at=FIXED_NOW,
                last_item_at=None,
                deactivated_at=FIXED_NOW - timedelta(days=2),
                deactivation_reason=REASON_FETCH_FAILURES,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert [(e.action, e.reason) for e in result.reactivated] == [
            (ACTION_ACTIVATED, REASON_RECOVERED)
        ]
        assert bool(row.active) is True
        assert row.deactivated_at is None
        assert row.deactivation_reason is None

        with curated_engine.begin() as conn:
            assert (ACTION_ACTIVATED, REASON_RECOVERED) in _events(conn, sid)

    def test_reactivates_silent_source_only_after_a_fresh_item(
        self, curated_engine: Engine
    ) -> None:
        # A silence-deactivated source that only fetches cleanly again is
        # not yet proof it's producing content. Only reactivate once a
        # fresh item lands within the silent window.
        with curated_engine.begin() as conn:
            no_items = _insert_source(
                conn,
                url="https://quiet.example/feed",
                active=False,
                consecutive_fetch_failures=0,
                last_fetch_ok_at=FIXED_NOW,
                last_item_at=None,
                deactivated_at=FIXED_NOW - timedelta(days=2),
                deactivation_reason=REASON_SILENT,
            )
            with_item = _insert_source(
                conn,
                url="https://talking.example/feed",
                active=False,
                consecutive_fetch_failures=0,
                last_fetch_ok_at=FIXED_NOW,
                last_item_at=FIXED_NOW - timedelta(days=1),
                deactivated_at=FIXED_NOW - timedelta(days=2),
                deactivation_reason=REASON_SILENT,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            no_items_row = _source_row(conn, no_items)
            with_item_row = _source_row(conn, with_item)

        reactivated_ids = {e.source_id for e in result.reactivated}
        assert reactivated_ids == {with_item}
        assert bool(no_items_row.active) is False
        assert bool(with_item_row.active) is True

    def test_does_not_touch_operator_disabled_sources(self, curated_engine: Engine) -> None:
        # A source that is inactive but was never touched by prune
        # (``deactivated_at`` is NULL) must be left alone — that's the
        # ``sources disable`` CLI path.
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://operator-off.example/feed",
                active=False,
                consecutive_fetch_failures=0,
                last_fetch_ok_at=FIXED_NOW,
                last_item_at=FIXED_NOW,
                deactivated_at=None,
                deactivation_reason=None,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert result.reactivated == []
        assert bool(row.active) is False

    def test_does_not_reactivate_while_still_failing(self, curated_engine: Engine) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(
                conn,
                url="https://still-broken.example/feed",
                active=False,
                consecutive_fetch_failures=6,
                last_fetch_ok_at=None,
                last_item_at=None,
                deactivated_at=FIXED_NOW - timedelta(days=2),
                deactivation_reason=REASON_FETCH_FAILURES,
            )

            result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row = _source_row(conn, sid)

        assert result.reactivated == []
        assert bool(row.active) is False


# ---------------------------------------------------------------------------
# End-to-end lifecycle: ingest -> prune -> probe -> prune
# ---------------------------------------------------------------------------


class TestLifecycleEndToEnd:
    def test_broken_source_gets_deactivated_then_reactivated_after_probe_recovery(
        self, curated_engine: Engine
    ) -> None:
        with curated_engine.begin() as conn:
            sid = _insert_source(conn, url="https://flaky.example/feed")

        # First, N failed fetch cycles push the source over the threshold.
        def failing(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with _mock_client(failing) as client:
            for _ in range(5):
                with curated_engine.begin() as conn:
                    ingest_all_active(conn, client=client, now=FIXED_NOW)

        # Prune deactivates it.
        with curated_engine.begin() as conn:
            deactivate_result = prune_sources(
                conn,
                now=FIXED_NOW,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row_after_dead = _source_row(conn, sid)

        assert [e.source_id for e in deactivate_result.deactivated] == [sid]
        assert bool(row_after_dead.active) is False
        assert row_after_dead.deactivation_reason == REASON_FETCH_FAILURES

        # The source recovers — a probe finds it healthy again.
        def healthy(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_fixture("example_rss.xml"))

        later = FIXED_NOW + timedelta(days=1)
        with curated_engine.begin() as conn, _mock_client(healthy) as client:
            probe_inactive_sources(conn, client=client, now=later)

        # Prune brings it back.
        with curated_engine.begin() as conn:
            reactivate_result = prune_sources(
                conn,
                now=later,
                max_consecutive_failures=5,
                silent_weeks=8,
            )
            row_after_alive = _source_row(conn, sid)

        assert [e.source_id for e in reactivate_result.reactivated] == [sid]
        assert bool(row_after_alive.active) is True
        assert row_after_alive.deactivated_at is None
        assert row_after_alive.deactivation_reason is None

        # The audit log has both events in order.
        with curated_engine.begin() as conn:
            events = _events(conn, sid)
        assert events == [
            (ACTION_DEACTIVATED, REASON_FETCH_FAILURES),
            (ACTION_ACTIVATED, REASON_RECOVERED),
        ]


# ---------------------------------------------------------------------------
# Defaults + input validation
# ---------------------------------------------------------------------------


def test_default_thresholds_are_reasonable() -> None:
    assert DEFAULT_MAX_CONSECUTIVE_FAILURES >= 3
    assert DEFAULT_SILENT_WEEKS >= 4


def test_prune_rejects_nonpositive_thresholds(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        with pytest.raises(ValueError):
            prune_sources(conn, now=FIXED_NOW, max_consecutive_failures=0)
        with pytest.raises(ValueError):
            prune_sources(conn, now=FIXED_NOW, silent_weeks=0)
