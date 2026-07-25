"""Tests for the weekly APScheduler wiring and ``signalweek schedule`` CLI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from signalweek.cli import build_parser, main
from signalweek.db import Issue, SignalItem, create_session_factory
from signalweek.scheduler import (
    JOB_ID,
    WEEKLY_TRIGGER_DAY,
    WEEKLY_TRIGGER_HOUR,
    WEEKLY_TRIGGER_MINUTE,
    WEEKLY_TRIGGER_TIMEZONE,
    build_scheduler,
    run_weekly_job,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)  # Saturday, ISO 2026-W30
WEEK_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


class TestRunWeeklyJob:
    async def test_runs_ingest_then_digest_and_returns_issue(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)
        call_order: list[str] = []

        async def fake_ingest(session: AsyncSession) -> None:
            call_order.append("ingest")
            session.add(
                SignalItem(
                    url="https://example.test/a",
                    title="Kubernetes networking deep dive",
                    source="Hacker News",
                    published_at=WEEK_START + timedelta(hours=6),
                )
            )
            session.add(
                SignalItem(
                    url="https://example.test/b",
                    title="Rust async runtimes compared",
                    source="Hacker News",
                    published_at=WEEK_START + timedelta(days=1),
                )
            )
            await session.flush()

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            call_order.append("digest")
            from signalweek.digest import assemble_digest

            return await assemble_digest(session, now=now)

        issue = await run_weekly_job(
            factory,
            now=NOW,
            ingest_fn=fake_ingest,
            digest_fn=fake_digest,
        )

        assert call_order == ["ingest", "digest"]
        assert issue.number == 202630
        assert issue.title == "SignalWeek 2026-W30"
        assert {item.url for item in issue.items} == {
            "https://example.test/a",
            "https://example.test/b",
        }

        # Verify the issue was actually persisted (fresh session, no cache).
        verify_factory = create_session_factory(engine)
        async with verify_factory() as session:
            rows = (await session.execute(select(Issue))).scalars().all()
            assert len(rows) == 1
            assert rows[0].number == 202630

    async def test_shares_a_single_session_between_ingest_and_digest(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)
        seen: list[AsyncSession] = []

        async def fake_ingest(session: AsyncSession) -> None:
            seen.append(session)

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            seen.append(session)
            issue = Issue(number=1, title="stub", body_markdown="", published_at=now)
            session.add(issue)
            await session.commit()
            return issue

        await run_weekly_job(factory, now=NOW, ingest_fn=fake_ingest, digest_fn=fake_digest)
        assert len(seen) == 2
        assert seen[0] is seen[1]

    async def test_defaults_to_now_in_utc_when_omitted(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)
        observed: dict[str, Any] = {}

        async def fake_ingest(session: AsyncSession) -> None:
            return None

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            observed["now"] = now
            issue = Issue(number=1, title="stub", body_markdown="", published_at=now)
            session.add(issue)
            await session.commit()
            return issue

        await run_weekly_job(factory, ingest_fn=fake_ingest, digest_fn=fake_digest)
        assert observed["now"].tzinfo is not None
        assert observed["now"].utcoffset() == timedelta(0)

    async def test_normalizes_naive_now_to_utc(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        observed: dict[str, Any] = {}

        async def fake_ingest(session: AsyncSession) -> None:
            return None

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            observed["now"] = now
            issue = Issue(number=1, title="stub", body_markdown="", published_at=now)
            session.add(issue)
            await session.commit()
            return issue

        naive = datetime(2026, 7, 25, 12, 0)
        await run_weekly_job(
            factory, now=naive, ingest_fn=fake_ingest, digest_fn=fake_digest
        )
        assert observed["now"] == datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    async def test_propagates_ingest_failure(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        digest_called = False

        async def failing_ingest(session: AsyncSession) -> None:
            raise RuntimeError("upstream down")

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            nonlocal digest_called
            digest_called = True
            return Issue(number=0, title="", body_markdown="", published_at=now)

        with pytest.raises(RuntimeError, match="upstream down"):
            await run_weekly_job(
                factory, now=NOW, ingest_fn=failing_ingest, digest_fn=fake_digest
            )
        assert digest_called is False


class TestBuildScheduler:
    def test_registers_single_weekly_job(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)
        scheduler = build_scheduler(factory)

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == JOB_ID
        assert jobs[0].func is run_weekly_job

    def test_trigger_fires_on_monday_09_00_utc(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)
        scheduler = build_scheduler(factory)

        job = scheduler.get_job(JOB_ID)
        assert job is not None
        trigger = job.trigger
        assert isinstance(trigger, CronTrigger)
        assert str(trigger.timezone) == WEEKLY_TRIGGER_TIMEZONE

        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["day_of_week"] == WEEKLY_TRIGGER_DAY
        assert fields["hour"] == str(WEEKLY_TRIGGER_HOUR)
        assert fields["minute"] == str(WEEKLY_TRIGGER_MINUTE)

        # Verify the next fire actually lands on a Monday at 09:00 UTC.
        from_when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)  # Saturday
        next_fire = trigger.get_next_fire_time(None, from_when)
        assert next_fire is not None
        assert next_fire.utcoffset() == timedelta(0)
        assert next_fire.isoweekday() == 1  # Monday
        assert next_fire.hour == 9
        assert next_fire.minute == 0
        # It's the *coming* Monday, not one further out.
        assert next_fire == datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


class TestCli:
    def test_parser_exposes_schedule_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["schedule"])
        assert args.command == "schedule"
        assert callable(args.func)

    def test_main_without_command_exits(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    def test_schedule_command_starts_and_stops_via_injected_runner(
        self,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`signalweek schedule` should hand off to the async runner."""

        called: dict[str, Any] = {}

        async def fake_runner(database_url: str) -> None:
            called["database_url"] = database_url

        monkeypatch.setattr("signalweek.cli._run_scheduler_forever", fake_runner)
        monkeypatch.setenv("SIGNALWEEK_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

        rc = main(["schedule"])
        assert rc == 0
        assert called["database_url"] == "sqlite+aiosqlite:///:memory:"


class TestSchedulerRunsRealJob:
    """End-to-end: fire the registered job directly and verify the DB."""

    async def test_triggering_job_function_writes_issue(
        self, engine: AsyncEngine
    ) -> None:
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)

        async def seed_ingest(session: AsyncSession) -> None:
            session.add(
                SignalItem(
                    url="https://example.test/only",
                    title="Distributed consensus made simple",
                    source="Hacker News",
                    published_at=WEEK_START + timedelta(hours=3),
                )
            )
            await session.flush()

        scheduler = build_scheduler(factory, ingest_fn=seed_ingest)
        job = scheduler.get_job(JOB_ID)
        assert job is not None

        # Call the job's callable directly with a pinned ``now`` — this is
        # exactly the shape the task asks tests to exercise.
        issue = await job.func(
            session_factory=factory,
            now=NOW,
            ingest_fn=seed_ingest,
            digest_fn=job.kwargs["digest_fn"],
        )
        assert issue.number == 202630
        assert issue.title == "SignalWeek 2026-W30"
        assert [item.url for item in issue.items] == ["https://example.test/only"]
