"""Tests for the weekly digest scheduler and materialization."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from signalweek.db.base import Base
from signalweek.db.models import Digest
from signalweek.db.repositories import (
    DigestRepository,
    SignalRepository,
    SourceRepository,
    UserRepository,
)
from signalweek.db.session import create_db_engine, create_session_factory
from signalweek.scheduler import (
    WEEKLY_JOB_ID,
    build_scheduler,
    materialize_week,
    previous_week_window,
    run_weekly_job,
)

WINDOW_START = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)  # Monday
WINDOW_END = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)  # Monday
NOW = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A session factory bound to a fresh temp-file SQLite database.

    A file-backed database is used (rather than ``:memory:``) so that multiple
    sessions minted by the factory — as the scheduler and CLI do — see the
    same rows.
    """
    engine = create_db_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_user_with_signals(session: Session, *, email: str) -> int:
    user = UserRepository(session).create(email=email, hashed_password="x")
    source = SourceRepository(session).create(
        user_id=user.id, url=f"https://feed.example/{email}", title=email
    )
    SignalRepository(session).create(
        source_id=source.id,
        guid=f"{email}-1",
        title=f"News for {email}",
        url=f"https://feed.example/{email}/1",
        summary="A story.",
        published_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )
    return user.id


# ---------------------------------------------------------------------------
# previous_week_window
# ---------------------------------------------------------------------------


def test_previous_week_window_from_midweek() -> None:
    # Wednesday 2026-07-22 12:34 UTC → week [Mon 2026-07-13, Mon 2026-07-20).
    now = datetime(2026, 7, 22, 12, 34, tzinfo=UTC)
    start, end = previous_week_window(now)
    assert start == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def test_previous_week_window_at_monday_midnight() -> None:
    # At the scheduled fire time the just-closed week is the previous seven days.
    now = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    start, end = previous_week_window(now)
    assert start == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def test_previous_week_window_naive_datetime_assumed_utc() -> None:
    start, end = previous_week_window(datetime(2026, 7, 22, 12, 34))
    assert start.tzinfo == UTC and end.tzinfo == UTC
    assert start == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


def test_previous_week_window_converts_non_utc_zone() -> None:
    # Tokyo time 2026-07-20 08:30 (+09:00) is UTC 2026-07-19 23:30, still a Sunday.
    tokyo = datetime(2026, 7, 20, 8, 30, tzinfo=timezone(timedelta(hours=9)))
    start, end = previous_week_window(tokyo)
    assert start == datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# materialize_week
# ---------------------------------------------------------------------------


def test_materialize_week_creates_a_row_per_active_user(session: Session) -> None:
    alice_id = _seed_user_with_signals(session, email="alice@example.com")
    bob_id = _seed_user_with_signals(session, email="bob@example.com")
    session.commit()

    ids = materialize_week(session, window_start=WINDOW_START, window_end=WINDOW_END, now=NOW)
    session.commit()

    assert len(ids) == 2
    digest_repo = DigestRepository(session)
    for user_id in (alice_id, bob_id):
        row = digest_repo.get_for_week(user_id, WINDOW_START.date())
        assert row is not None
        assert row.week_start == WINDOW_START.date()
        assert "News for" in row.content


def test_materialize_week_is_idempotent(session: Session) -> None:
    _seed_user_with_signals(session, email="alice@example.com")
    session.commit()

    first = materialize_week(session, window_start=WINDOW_START, window_end=WINDOW_END, now=NOW)
    session.commit()
    second = materialize_week(session, window_start=WINDOW_START, window_end=WINDOW_END, now=NOW)
    session.commit()

    assert len(first) == 1
    assert second == []
    total = session.execute(select(func.count()).select_from(Digest)).scalar_one()
    assert total == 1


def test_materialize_week_skips_inactive_users(session: Session) -> None:
    active_id = _seed_user_with_signals(session, email="active@example.com")
    inactive = UserRepository(session).create(
        email="inactive@example.com", hashed_password="x", is_active=False
    )
    session.commit()

    ids = materialize_week(session, window_start=WINDOW_START, window_end=WINDOW_END, now=NOW)
    session.commit()

    digest_repo = DigestRepository(session)
    assert digest_repo.get_for_week(active_id, WINDOW_START.date()) is not None
    assert digest_repo.get_for_week(inactive.id, WINDOW_START.date()) is None
    assert len(ids) == 1


def test_materialize_week_creates_empty_digest_for_user_with_no_sources(
    session: Session,
) -> None:
    lonely = UserRepository(session).create(email="lonely@example.com", hashed_password="x")
    session.commit()

    ids = materialize_week(session, window_start=WINDOW_START, window_end=WINDOW_END, now=NOW)
    session.commit()

    assert len(ids) == 1
    row = DigestRepository(session).get_for_week(lonely.id, WINDOW_START.date())
    assert row is not None and row.content  # rendered empty-state template


def test_materialize_week_uses_custom_renderer(session: Session) -> None:
    _seed_user_with_signals(session, email="alice@example.com")
    session.commit()

    materialize_week(
        session,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW,
        render=lambda d: f"canned:{d.user_email}",
    )
    session.commit()

    row = session.execute(select(Digest)).scalar_one()
    assert row.content == "canned:alice@example.com"


# ---------------------------------------------------------------------------
# run_weekly_job
# ---------------------------------------------------------------------------


def test_run_weekly_job_defaults_to_previous_week(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        _seed_user_with_signals(s, email="alice@example.com")
        s.commit()

    monday = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)  # cron fire time
    ids = run_weekly_job(session_factory=db_factory, now=monday)

    assert len(ids) == 1
    with db_factory() as s:
        rows = list(s.execute(select(Digest)).scalars())
        assert len(rows) == 1
        assert rows[0].week_start == date(2026, 7, 13)


def test_run_weekly_job_accepts_explicit_week_start(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        _seed_user_with_signals(s, email="alice@example.com")
        s.commit()

    ids = run_weekly_job(
        session_factory=db_factory,
        week_start=date(2026, 7, 13),
        now=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
    )
    assert len(ids) == 1
    with db_factory() as s:
        row = s.execute(select(Digest)).scalar_one()
        assert row.week_start == date(2026, 7, 13)


def test_run_weekly_job_is_idempotent_across_calls(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        _seed_user_with_signals(s, email="alice@example.com")
        s.commit()

    monday = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    first = run_weekly_job(session_factory=db_factory, now=monday)
    second = run_weekly_job(session_factory=db_factory, now=monday)

    assert len(first) == 1
    assert second == []
    with db_factory() as s:
        total = s.execute(select(func.count()).select_from(Digest)).scalar_one()
        assert total == 1


def test_run_weekly_job_rolls_back_on_error(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        _seed_user_with_signals(s, email="alice@example.com")
        s.commit()

    def boom(_digest: object) -> str:
        raise RuntimeError("render blew up")

    with pytest.raises(RuntimeError, match="render blew up"):
        run_weekly_job(
            session_factory=db_factory,
            week_start=date(2026, 7, 13),
            now=NOW,
            render=boom,
        )

    with db_factory() as s:
        total = s.execute(select(func.count()).select_from(Digest)).scalar_one()
        assert total == 0


# ---------------------------------------------------------------------------
# build_scheduler
# ---------------------------------------------------------------------------


def test_build_scheduler_registers_monday_midnight_cron() -> None:
    scheduler = build_scheduler()
    job = scheduler.get_job(WEEKLY_JOB_ID)
    assert job is not None
    assert job.func is run_weekly_job
    assert isinstance(job.trigger, CronTrigger)

    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon"
    assert fields["hour"] == "0"
    assert fields["minute"] == "0"
    assert str(job.trigger.timezone) == "UTC"


def test_build_scheduler_forwards_session_factory(
    db_factory: sessionmaker[Session],
) -> None:
    scheduler = build_scheduler(session_factory=db_factory)
    job = scheduler.get_job(WEEKLY_JOB_ID)
    assert job is not None
    assert job.kwargs == {"session_factory": db_factory}
