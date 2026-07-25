"""Weekly digest scheduler.

Three pieces live here:

* :func:`previous_week_window` — pure helper returning the
  ``[Monday_prev 00:00 UTC, Monday_this 00:00 UTC)`` window ending at ``now``.
* :func:`materialize_week` — iterates active users and persists a
  :class:`~signalweek.db.models.Digest` row per user. Idempotent per
  ``(user_id, week_start)``.
* :func:`run_weekly_job` — self-contained callable used by both APScheduler and
  the ``signalweek run-week`` CLI. Owns the session lifecycle.
* :func:`build_scheduler` — a :class:`BackgroundScheduler` with the Monday
  00:00 UTC cron job attached.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from signalweek.db.repositories import DigestRepository, UserRepository
from signalweek.db.session import get_session_factory
from signalweek.digest import Digest, build_digest, render_html

WEEKLY_JOB_ID = "signalweek-weekly-digests"

DigestRenderer = Callable[[Digest], str]
SessionFactory = Callable[[], Session]


def previous_week_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the ``[Monday_prev, Monday_this)`` window ending at ``now``'s Monday.

    Fired at Monday 00:00 UTC the returned range spans the seven days that
    just closed. Naive datetimes are assumed to be UTC.
    """
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    aware = aware.astimezone(UTC)
    start_of_day = aware.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = start_of_day - timedelta(days=aware.weekday())
    return this_monday - timedelta(days=7), this_monday


def materialize_week(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
    render: DigestRenderer | None = None,
) -> list[int]:
    """Build and persist a digest row for every active user in the given week.

    Users that already have a digest for ``window_start.date()`` are skipped
    (so re-running the job is safe). Inactive users are also skipped. Returns
    the primary keys of newly-created rows in creation order.
    """
    renderer = render if render is not None else render_html
    week_start = window_start.date()
    users = UserRepository(session).list()
    digest_repo = DigestRepository(session)
    created: list[int] = []
    for user in users:
        if not user.is_active:
            continue
        if digest_repo.get_for_week(user.id, week_start) is not None:
            continue
        digest = build_digest(
            session,
            user,
            window_start=window_start,
            window_end=window_end,
            now=now,
        )
        row = digest_repo.create(
            user_id=user.id,
            week_start=week_start,
            content=renderer(digest),
        )
        created.append(row.id)
    return created


def run_weekly_job(
    *,
    session_factory: SessionFactory | None = None,
    week_start: date | None = None,
    now: datetime | None = None,
    render: DigestRenderer | None = None,
) -> list[int]:
    """Materialize a week's digests, owning the session lifecycle.

    When ``week_start`` is omitted the previous full week (ending at ``now``,
    defaulting to :func:`datetime.now`) is used — matching the scheduled fire
    time of Monday 00:00 UTC.
    """
    factory = session_factory if session_factory is not None else get_session_factory()
    resolved_now = now if now is not None else datetime.now(UTC)
    if week_start is None:
        window_start, window_end = previous_week_window(resolved_now)
    else:
        window_start = datetime.combine(week_start, time.min, tzinfo=UTC)
        window_end = window_start + timedelta(days=7)
    session = factory()
    try:
        created = materialize_week(
            session,
            window_start=window_start,
            window_end=window_end,
            now=resolved_now,
            render=render,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return created


def build_scheduler(
    session_factory: SessionFactory | None = None,
) -> BackgroundScheduler:
    """Return a :class:`BackgroundScheduler` with the Monday 00:00 UTC job attached.

    Call ``.start()`` on the returned scheduler to begin execution and
    ``.shutdown()`` at process exit. Passing ``session_factory`` is useful for
    tests; in production the job falls back to the process-wide factory.
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    kwargs: dict[str, object] = {}
    if session_factory is not None:
        kwargs["session_factory"] = session_factory
    scheduler.add_job(
        run_weekly_job,
        trigger=CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC"),
        id=WEEKLY_JOB_ID,
        replace_existing=True,
        kwargs=kwargs,
    )
    return scheduler
