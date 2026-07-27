"""Tests for :mod:`signalweek.scheduler`.

The scheduler wires two APScheduler jobs:

* An hourly ingest that pulls active feeds and rebuilds clusters.
* A Monday 09:00 America/New_York build → verify → publish pipeline.

Tests cover the trigger configuration (hourly UTC + weekly NY),
DST safety of the weekly trigger, the absence of any email-send job
(deferred), and the orchestration semantics of :func:`run_ingest` and
:func:`run_weekly_pipeline`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import sessionmaker

from signalweek.db.session import create_db_engine
from signalweek.digest.builder import IssueAlreadyExistsError
from signalweek.ingest.feeds import IngestRunResult, SourceIngestResult
from signalweek.scheduler import (
    INGEST_JOB_ID,
    WEEKLY_JOB_ID,
    WEEKLY_PIPELINE_DAY_OF_WEEK,
    WEEKLY_PIPELINE_HOUR,
    WEEKLY_PIPELINE_MINUTE,
    WEEKLY_PIPELINE_TIMEZONE,
    create_scheduler,
    run_ingest,
    run_weekly_pipeline,
)
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)

NY = ZoneInfo("America/New_York")


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


@pytest.fixture()
def session_factory(curated_engine: Engine):
    return sessionmaker(bind=curated_engine, autoflush=False, expire_on_commit=False)


def _seed_publishable_state(conn: Connection, *, now: datetime | None = None) -> None:
    """Seed enough recent clusters that the weekly build will publish.

    ``now`` controls the ``first_seen_at`` stamp on the seeded raw_items;
    tests that invoke the pipeline with the real wall clock should pass
    ``datetime.now(UTC)`` so the items stay inside the 7-day lookback.
    """
    if now is None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    result = conn.execute(
        sources_table.insert()
        .values(url="https://openai.com/blog/rss.xml", kind="rss", category_hint="models")
        .returning(sources_table.c.id)
    )
    source_id = int(result.scalar_one())

    seeds = [
        ("models", "OpenAI unveils GPT-5", "https://openai.com/blog/gpt-5"),
        ("models", "Anthropic releases Claude 5", "https://openai.com/blog/claude-5"),
        ("models", "Google launches Gemini 3", "https://openai.com/blog/gemini-3"),
        (
            "funding",
            "Anthropic raises Series F",
            "https://openai.com/blog/anthropic-round",
        ),
        (
            "funding",
            "Startup raises Series C",
            "https://openai.com/blog/startup-round",
        ),
        (
            "lawsuits_policy",
            "Court rules against AI vendor",
            "https://openai.com/blog/court-ruling",
        ),
        (
            "lawsuits_policy",
            "Executive order signed on AI safety",
            "https://openai.com/blog/eo-ai",
        ),
        (
            "research",
            "New preprint proposes benchmark",
            "https://openai.com/blog/preprint",
        ),
        (
            "research",
            "Researchers report SOTA",
            "https://openai.com/blog/sota",
        ),
        (
            "industry_moves",
            "Google hires new AI CEO",
            "https://openai.com/blog/hire-ceo",
        ),
        (
            "industry_moves",
            "Meta announces AI layoffs",
            "https://openai.com/blog/layoffs",
        ),
    ]
    for category, headline, url in seeds:
        conn.execute(
            raw_items_table.insert().values(
                source_id=source_id,
                url=url,
                canonical_url=url,
                title=headline,
                body=f"{headline}. Extended lede content.",
                fetched_at=now,
                first_seen_at=now,
            )
        )
        conn.execute(
            clusters_table.insert().values(
                primary_url=url,
                canonical_headline=headline,
                category=category,
            )
        )


# ---------------------------------------------------------------------------
# Job wiring
# ---------------------------------------------------------------------------


class TestSchedulerConfiguration:
    def test_registers_exactly_two_jobs(self) -> None:
        sched = create_scheduler(scheduler=BackgroundScheduler(timezone=UTC))
        job_ids = {job.id for job in sched.get_jobs()}
        assert job_ids == {INGEST_JOB_ID, WEEKLY_JOB_ID}

    def test_no_email_send_job_is_registered(self) -> None:
        """Email delivery is deferred; the scheduler must not ship a send job."""
        sched = create_scheduler(scheduler=BackgroundScheduler(timezone=UTC))
        for job in sched.get_jobs():
            lowered = f"{job.id} {job.name}".lower()
            assert "send" not in lowered
            assert "email" not in lowered
            assert "smtp" not in lowered
            assert "mail" not in lowered

    def test_ingest_job_uses_hourly_cron(self) -> None:
        sched = create_scheduler(scheduler=BackgroundScheduler(timezone=UTC))
        job = sched.get_job(INGEST_JOB_ID)
        assert isinstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        # Every hour, on the hour.
        assert fields["minute"] == "0"
        assert fields["hour"] == "*"
        assert fields["day"] == "*"
        assert fields["day_of_week"] == "*"

    def test_weekly_job_uses_monday_9am_new_york(self) -> None:
        sched = create_scheduler(scheduler=BackgroundScheduler(timezone=UTC))
        job = sched.get_job(WEEKLY_JOB_ID)
        assert isinstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["day_of_week"] == WEEKLY_PIPELINE_DAY_OF_WEEK
        assert fields["hour"] == str(WEEKLY_PIPELINE_HOUR)
        assert fields["minute"] == str(WEEKLY_PIPELINE_MINUTE)
        assert job.trigger.timezone == WEEKLY_PIPELINE_TIMEZONE


# ---------------------------------------------------------------------------
# DST safety
# ---------------------------------------------------------------------------


class TestDstSafety:
    def _weekly_trigger(self) -> CronTrigger:
        return CronTrigger(
            day_of_week=WEEKLY_PIPELINE_DAY_OF_WEEK,
            hour=WEEKLY_PIPELINE_HOUR,
            minute=WEEKLY_PIPELINE_MINUTE,
            timezone=WEEKLY_PIPELINE_TIMEZONE,
        )

    def _walk(self, trigger: CronTrigger, *, start: datetime, count: int) -> list[datetime]:
        fires: list[datetime] = []
        previous: datetime | None = None
        now = start
        for _ in range(count):
            previous = trigger.get_next_fire_time(previous, now)
            assert previous is not None
            fires.append(previous)
            now = previous + timedelta(seconds=1)
        return fires

    def test_weekly_trigger_always_fires_at_local_9am_monday(self) -> None:
        """DST transitions must not move the wall-clock hour of the job."""
        # Walk from early February 2026 through mid-June 2026, which straddles
        # the US spring-forward transition on 2026-03-08.
        fires = self._walk(
            self._weekly_trigger(),
            start=datetime(2026, 2, 1, tzinfo=NY),
            count=20,
        )
        for fire in fires:
            local = fire.astimezone(NY)
            assert local.hour == 9
            assert local.minute == 0
            assert local.weekday() == 0, f"expected Monday, got {local}"

    def test_weekly_trigger_spans_est_and_edt_offsets(self) -> None:
        """The same 20-week walk must see both -05:00 and -04:00 UTC offsets."""
        fires = self._walk(
            self._weekly_trigger(),
            start=datetime(2026, 2, 1, tzinfo=NY),
            count=20,
        )
        offsets = {fire.utcoffset() for fire in fires}
        assert timedelta(hours=-5) in offsets, "expected an EST firing"
        assert timedelta(hours=-4) in offsets, "expected an EDT firing"

    def test_weekly_trigger_fall_back_keeps_9am_local(self) -> None:
        """Fall-back (EDT → EST) must not double-fire or shift the wall clock."""
        fires = self._walk(
            self._weekly_trigger(),
            start=datetime(2026, 10, 15, tzinfo=NY),
            count=6,
        )
        # Every firing is 09:00 local NY, one week apart.
        for fire in fires:
            assert fire.astimezone(NY).hour == 9
        deltas = {(b - a) for a, b in zip(fires, fires[1:], strict=False)}
        # Weekly steps: 7 days on same-DST weeks, 7d ± 1h across the boundary.
        assert deltas.issubset(
            {
                timedelta(days=7),
                timedelta(days=7, hours=1),
                timedelta(days=7, hours=-1),
            }
        )


# ---------------------------------------------------------------------------
# run_ingest orchestration
# ---------------------------------------------------------------------------


class TestRunIngest:
    def test_calls_ingest_then_cluster_then_prune_and_commits_separately(
        self, session_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        ingest_stub = IngestRunResult(
            per_source=[
                SourceIngestResult(
                    source_id=1, url="https://example.com/rss", inserted=3, skipped=1
                )
            ]
        )

        def fake_ingest_all_active(session, **kwargs):
            calls.append("ingest")
            return ingest_stub

        def fake_cluster(session, **kwargs):
            calls.append("cluster")
            from signalweek.ingest.cluster import ClusterRunResult

            return ClusterRunResult()

        def fake_prune(session, **kwargs):
            calls.append("prune")
            from signalweek.ingest.health import PruneResult

            return PruneResult()

        monkeypatch.setattr("signalweek.scheduler.ingest_all_active", fake_ingest_all_active)
        monkeypatch.setattr("signalweek.scheduler.cluster_raw_items", fake_cluster)
        monkeypatch.setattr("signalweek.scheduler.prune_sources", fake_prune)

        result = run_ingest(session_factory)

        assert calls == ["ingest", "cluster", "prune"]
        assert result is ingest_stub

    def test_returns_ingest_result_with_zero_active_sources(self, session_factory) -> None:
        """With no active sources the pipeline still completes cleanly."""
        result = run_ingest(session_factory)
        assert result.total_inserted == 0
        assert result.total_skipped == 0
        assert result.errors == []


# ---------------------------------------------------------------------------
# run_weekly_pipeline orchestration
# ---------------------------------------------------------------------------


class TestRunWeeklyPipeline:
    def test_publishes_and_verifies_when_enough_items(
        self, curated_engine: Engine, session_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with curated_engine.begin() as conn:
            _seed_publishable_state(conn)

        from signalweek.digest.verify import VerifyResult

        verify_calls: list[int] = []

        def spy_verify(session, *, issue_id, **kwargs):
            verify_calls.append(issue_id)
            # Skip real HTTP; return a passing result and leave items alone.
            return VerifyResult(issue_id=issue_id, checked=0, kept=0, dropped=0)

        monkeypatch.setattr("signalweek.scheduler.verify_issue", spy_verify)

        now = datetime(2026, 7, 27, 13, 0, tzinfo=UTC)  # Monday afternoon UTC
        result = run_weekly_pipeline(session_factory, now=now)

        assert result.build.status == "published"
        assert result.verify is not None
        assert verify_calls == [result.build.issue_id]

        # The build actually landed in the DB.
        with curated_engine.begin() as conn:
            issue = conn.execute(
                issues_table.select().where(issues_table.c.id == result.build.issue_id)
            ).one()
            item_count = conn.execute(
                items_table.select().where(items_table.c.issue_id == result.build.issue_id)
            ).all()
        assert issue.status == "published"
        assert issue.published_at is not None
        assert len(item_count) == result.build.total_items

    def test_holds_thin_issue_and_skips_verify(
        self, session_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An issue that would fall under the min-items floor is held; the
        verify step is not called on a held issue."""
        verify_calls: list[int] = []

        def unused_verify(session, *, issue_id, **kwargs):  # pragma: no cover - guard
            verify_calls.append(issue_id)
            raise AssertionError("verify must not run for held issues")

        monkeypatch.setattr("signalweek.scheduler.verify_issue", unused_verify)

        now = datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
        result = run_weekly_pipeline(session_factory, now=now)

        assert result.build.status == "held"
        assert result.verify is None
        assert verify_calls == []

    def test_rebuilding_same_week_reraises_issue_already_exists(
        self,
        curated_engine: Engine,
        session_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two ticks in the same ISO week — the second must raise cleanly rather
        than silently overwriting the previous issue."""
        with curated_engine.begin() as conn:
            _seed_publishable_state(conn)

        from signalweek.digest.verify import VerifyResult

        def fake_verify(session, *, issue_id, **kwargs):
            return VerifyResult(issue_id=issue_id, checked=0, kept=0, dropped=0)

        monkeypatch.setattr("signalweek.scheduler.verify_issue", fake_verify)

        now = datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
        run_weekly_pipeline(session_factory, now=now)

        with pytest.raises(IssueAlreadyExistsError):
            run_weekly_pipeline(session_factory, now=now)


# ---------------------------------------------------------------------------
# Integration: create_scheduler wires the real functions
# ---------------------------------------------------------------------------


class TestSchedulerJobsRunEndToEnd:
    def test_weekly_job_fires_build_verify_publish_when_manually_invoked(
        self, curated_engine: Engine, session_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Manually invoke the callable APScheduler would run and confirm the
        DB reaches the expected state — no background thread involved."""
        # The wrapped callable resolves ``now`` from the real wall clock, so
        # seed raw_items in the current 7-day window rather than a fixed date.
        wall_now = datetime.now(UTC)
        with curated_engine.begin() as conn:
            _seed_publishable_state(conn, now=wall_now)

        from signalweek.digest.verify import VerifyResult

        def fake_verify(session, *, issue_id, **kwargs):
            return VerifyResult(issue_id=issue_id, checked=0, kept=0, dropped=0)

        monkeypatch.setattr("signalweek.scheduler.verify_issue", fake_verify)

        sched = create_scheduler(
            session_factory=session_factory,
            scheduler=BackgroundScheduler(timezone=UTC),
        )
        weekly_job = sched.get_job(WEEKLY_JOB_ID)
        weekly_job.func()  # Runs run_weekly_pipeline synchronously.

        with curated_engine.begin() as conn:
            issues = conn.execute(issues_table.select()).all()
        assert len(issues) == 1
        assert issues[0].status == "published"
        # week_of is the Monday of the current ISO week.
        expected_week = wall_now.date() - timedelta(days=wall_now.weekday())
        assert issues[0].week_of == expected_week

    def test_ingest_job_callable_runs_without_active_sources(self, session_factory) -> None:
        sched = create_scheduler(
            session_factory=session_factory,
            scheduler=BackgroundScheduler(timezone=UTC),
        )
        ingest_job = sched.get_job(INGEST_JOB_ID)
        # Should not raise even though there are no active sources.
        ingest_job.func()

    def test_weekly_job_callable_swallows_duplicate_week_error(
        self, curated_engine: Engine, session_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The APScheduler-facing callable must not propagate
        ``IssueAlreadyExistsError`` — otherwise the trigger would be
        marked failed and the job could be removed by the executor."""
        with curated_engine.begin() as conn:
            _seed_publishable_state(conn, now=datetime.now(UTC))

        from signalweek.digest.verify import VerifyResult

        def fake_verify(session, *, issue_id, **kwargs):
            return VerifyResult(issue_id=issue_id, checked=0, kept=0, dropped=0)

        monkeypatch.setattr("signalweek.scheduler.verify_issue", fake_verify)

        sched = create_scheduler(
            session_factory=session_factory,
            scheduler=BackgroundScheduler(timezone=UTC),
        )
        weekly_job = sched.get_job(WEEKLY_JOB_ID)
        weekly_job.func()  # first tick creates the issue
        # Second tick would raise IssueAlreadyExistsError inside
        # run_weekly_pipeline, but the wrapping callable must swallow it.
        weekly_job.func()
