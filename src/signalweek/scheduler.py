"""APScheduler wiring for the two background jobs Signalweek needs.

The MVP runs as a single process, so a :class:`BackgroundScheduler` living
inside the app is enough — no external worker, no persistent job store. Two
jobs are registered:

* ``ingest`` — every hour on the hour (UTC). Fetches every active source
  and rebuilds clusters. Idempotent: running it more often just widens the
  raw-item catch-up window.
* ``weekly_pipeline`` — every Monday at 09:00 ``America/New_York``. The
  trigger is timezone-aware so it correctly follows the twice-yearly DST
  shift — 09:00 EST in winter and 09:00 EDT in summer, without a manual
  clock change. Runs build → verify → publish for the current week: the
  issue stays ``draft`` (never public) until dead links are removed.

Reliability (AIC-11): the weekly job has a 6 h misfire grace and coalesces,
both jobs share one lock so they never write to SQLite concurrently, every
execution is recorded in ``pipeline_runs``, and anything an operator must
know about lands in ``alerts``. On startup (and after every hourly ingest)
:func:`catch_up_weekly` builds this week's issue if its Monday slot has
passed without one, and :func:`record_missed_runs` writes a ``missed_run``
alert for every past week that has no issue. Past weeks are *not* rebuilt:
the builder reads today's cluster state, so a backfill would repeat stories
later issues already carried.

Email delivery, per-subscriber send, and the LLM support check are all
deferred to a later stage. No ``send`` job is registered here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from signalweek.db.session import create_session_factory, get_engine
from signalweek.digest.builder import (
    BuildResult,
    IssueAlreadyExistsError,
    build_issue,
    publish_issue,
)
from signalweek.digest.verify import VerifyResult, verify_issue
from signalweek.ingest.cluster import cluster_raw_items
from signalweek.ingest.feeds import IngestRunResult, ingest_all_active
from signalweek.ingest.health import prune_sources
from signalweek.sources import alerts_table, issues_table, pipeline_runs_table

logger = logging.getLogger(__name__)

# The weekly pipeline fires Monday 09:00 in New York. The trigger reads its
# wall clock in this zone, so DST transitions do not shift the local hour.
WEEKLY_PIPELINE_TIMEZONE = ZoneInfo("America/New_York")
WEEKLY_PIPELINE_DAY_OF_WEEK = "mon"
WEEKLY_PIPELINE_HOUR = 9
WEEKLY_PIPELINE_MINUTE = 0

INGEST_JOB_ID = "ingest"
WEEKLY_JOB_ID = "weekly_pipeline"
STARTUP_RECOVERY_JOB_ID = "startup_recovery"

# APScheduler's default misfire grace is 1 s: a Monday slot the scheduler
# thread woke up late for was silently dropped. A late weekly issue beats a
# missing one; an ingest tick more than 30 min late is just skipped.
WEEKLY_MISFIRE_GRACE_SECONDS = 6 * 60 * 60
INGEST_MISFIRE_GRACE_SECONDS = 30 * 60

# Spec criterion 16: the weekly pipeline must finish in under 30 minutes.
WEEKLY_RUNTIME_BUDGET = timedelta(minutes=30)

# Serialises every pipeline job in this process: SQLite allows one writer, and
# the hourly ingest fires in the same minute as the weekly slot. Re-entrant so
# catch-up can hold it across its check and the build.
_PIPELINE_LOCK = threading.RLock()

# Callable that returns a fresh :class:`Session` on every invocation. A
# stock ``sessionmaker`` satisfies this contract, which lets tests inject
# a factory bound to an in-memory engine.
SessionFactory = Callable[[], Session]


@dataclass
class WeeklyPipelineResult:
    """Outcome of one weekly pipeline tick.

    ``status`` is the issue's final status (``published`` or ``held``) and
    ``item_count`` its item count after verify. ``verify`` is ``None`` when
    the build was already too thin to publish — held issues stay in the
    database for an editor to inspect but are not verified or shipped.
    """

    build: BuildResult
    status: str
    item_count: int
    verify: VerifyResult | None = None


@dataclass
class _RunRecord:
    """Mutable outcome a job body fills in for its ``pipeline_runs`` row."""

    status: str = "ok"
    item_count: int | None = None
    week_of: date | None = None
    detail: str | None = None


def run_ingest(session_factory: SessionFactory) -> IngestRunResult:
    """Fetch every active source, rebuild clusters, then prune dead sources.

    The three writes are committed in separate transactions so a downstream
    failure does not roll back the raw_items that landed successfully.
    Pruning runs last so it observes the fresh fetch/success counters the
    ingest pass just wrote.
    """
    with _committed(session_factory) as session:
        ingest_result = ingest_all_active(session)
    with _committed(session_factory) as session:
        cluster_result = cluster_raw_items(session)
    with _committed(session_factory) as session:
        prune_result = prune_sources(session)

    logger.info(
        "ingest_tick inserted=%d skipped=%d errors=%d clusters_created=%d "
        "clusters_matched=%d deactivated=%d reactivated=%d",
        ingest_result.total_inserted,
        ingest_result.total_skipped,
        len(ingest_result.errors),
        cluster_result.created,
        cluster_result.matched,
        len(prune_result.deactivated),
        len(prune_result.reactivated),
    )
    for event in prune_result.deactivated:
        logger.info(
            "source_health_event source_id=%d url=%s action=deactivated reason=%s",
            event.source_id,
            event.url,
            event.reason,
        )
    for event in prune_result.reactivated:
        logger.info(
            "source_health_event source_id=%d url=%s action=activated reason=%s",
            event.source_id,
            event.url,
            event.reason,
        )
    return ingest_result


def run_weekly_pipeline(
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
    week_of: date | None = None,
) -> WeeklyPipelineResult:
    """Build the week's issue as a draft, verify its links, then publish.

    The issue only becomes ``published`` after verify has removed dead links
    and the survivors still clear the item floor; otherwise it is ``held``
    and an ``insufficient_items`` alert is recorded. When an issue for the
    target week already exists :class:`IssueAlreadyExistsError` propagates.
    """
    now = now or datetime.now(UTC)

    try:
        with _committed(session_factory) as session:
            build_result = build_issue(session, now=now, week_of=week_of, publish=False)
    except IssueAlreadyExistsError as exc:
        logger.info("weekly_pipeline skipped: %s", exc)
        raise

    status = build_result.status
    item_count = build_result.total_items
    verify_result: VerifyResult | None = None
    if status == "draft":
        with _committed(session_factory) as session:
            verify_result = verify_issue(session, issue_id=build_result.issue_id)
        with _committed(session_factory) as session:
            status, item_count = publish_issue(session, issue_id=build_result.issue_id, now=now)

    if status == "held":
        with _committed(session_factory) as session:
            record_alert(
                session,
                reason="insufficient_items",
                job=WEEKLY_JOB_ID,
                week_of=build_result.week_of,
                detail=(
                    f"issue_id={build_result.issue_id} held with {item_count} items "
                    f"(built {build_result.total_items}, "
                    f"dropped by verify {build_result.total_items - item_count})"
                ),
            )

    logger.info(
        "weekly_pipeline issue_id=%d week_of=%s status=%s items=%d verified=%s",
        build_result.issue_id,
        build_result.week_of.isoformat(),
        status,
        item_count,
        "yes" if verify_result is not None else "skipped",
    )
    return WeeklyPipelineResult(
        build=build_result, status=status, item_count=item_count, verify=verify_result
    )


def run_weekly_job(
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
    week_of: date | None = None,
) -> WeeklyPipelineResult | None:
    """Run the weekly pipeline as a tracked job; never raises.

    Records a ``pipeline_runs`` row (``skipped`` when the week already has an
    issue, ``failed`` plus a ``pipeline_failed`` alert when it raises).
    """
    now = now or datetime.now(UTC)
    week = week_of or _monday_of(now)
    outcome: list[WeeklyPipelineResult] = []

    def _body(run: _RunRecord) -> None:
        run.week_of = week
        try:
            result = run_weekly_pipeline(session_factory, now=now, week_of=week)
        except IssueAlreadyExistsError as exc:
            run.status = "skipped"
            run.detail = str(exc)
            return
        outcome.append(result)
        run.item_count = result.item_count
        run.detail = f"issue_id={result.build.issue_id} status={result.status}"

    with _PIPELINE_LOCK:
        _tracked_run(session_factory, WEEKLY_JOB_ID, _body)
    return outcome[0] if outcome else None


def run_ingest_job(session_factory: SessionFactory) -> None:
    """Run one tracked hourly ingest, then the weekly safety net; never raises."""

    def _body(run: _RunRecord) -> None:
        result = run_ingest(session_factory)
        run.item_count = result.total_inserted
        run.detail = f"skipped={result.total_skipped} errors={len(result.errors)}"

    with _PIPELINE_LOCK:
        _tracked_run(session_factory, INGEST_JOB_ID, _body)
    # Safety net while the process is up: if this week's slot passed and the
    # weekly job never even started, build now. A week whose run was
    # attempted and failed is left to its pipeline_failed alert rather than
    # retried every hour.
    try:
        catch_up_weekly(session_factory, require_no_attempt=True)
        record_missed_runs(session_factory)
    except Exception:
        logger.exception("weekly safety net failed")


def weekly_slot_due(now: datetime) -> datetime:
    """Return the most recent Monday 09:00 New York slot at or before ``now``."""
    local = now.astimezone(WEEKLY_PIPELINE_TIMEZONE)
    monday = local.date() - timedelta(days=local.weekday())
    slot = datetime.combine(
        monday,
        time(WEEKLY_PIPELINE_HOUR, WEEKLY_PIPELINE_MINUTE),
        tzinfo=WEEKLY_PIPELINE_TIMEZONE,
    )
    if slot > now:
        slot = datetime.combine(
            monday - timedelta(days=7),
            time(WEEKLY_PIPELINE_HOUR, WEEKLY_PIPELINE_MINUTE),
            tzinfo=WEEKLY_PIPELINE_TIMEZONE,
        )
    return slot


def catch_up_weekly(
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
    require_no_attempt: bool = False,
) -> WeeklyPipelineResult | None:
    """Build this ISO week's issue if its Monday slot has passed without one.

    Only the current (New York) week is caught up; a slot from an earlier
    week is reported by :func:`record_missed_runs` instead. With
    ``require_no_attempt`` the build is also skipped when a weekly run for
    the week was already recorded (whatever its outcome).
    """
    now = now or datetime.now(UTC)
    week = weekly_slot_due(now).date()
    current_monday = _ny_monday(now)
    if week != current_monday:
        return None
    with _PIPELINE_LOCK:
        with _committed(session_factory) as session:
            if _issue_exists(session, week):
                return None
            if (
                require_no_attempt
                and session.execute(
                    select(pipeline_runs_table.c.id).where(
                        pipeline_runs_table.c.job == WEEKLY_JOB_ID,
                        pipeline_runs_table.c.week_of == week,
                    )
                ).first()
            ):
                return None
        logger.warning(
            "weekly_pipeline catch_up week_of=%s (slot %s passed with no issue)",
            week.isoformat(),
            weekly_slot_due(now).isoformat(),
        )
        return run_weekly_job(session_factory, now=now, week_of=week)


def record_missed_runs(
    session_factory: SessionFactory, *, now: datetime | None = None
) -> list[date]:
    """Write a ``missed_run`` alert for every past week that has no issue.

    Scans from the first issue ever built up to the week before the current
    New York week. Idempotent: a week that already has a ``missed_run``
    alert is not reported again. Returns the newly reported weeks.
    """
    now = now or datetime.now(UTC)
    current_monday = _ny_monday(now)
    reported: list[date] = []
    with _committed(session_factory) as session:
        weeks = {row.week_of for row in session.execute(select(issues_table.c.week_of))}
        if not weeks:
            return reported
        already = {
            row.week_of
            for row in session.execute(
                select(alerts_table.c.week_of).where(alerts_table.c.reason == "missed_run")
            )
        }
        week = min(weeks)
        while week < current_monday:
            if week not in weeks and week not in already:
                record_alert(
                    session,
                    reason="missed_run",
                    job=WEEKLY_JOB_ID,
                    week_of=week,
                    detail=(
                        "no issue was built for this week; not backfilled because the "
                        "builder cannot reconstruct a past week from current cluster state"
                    ),
                    now=now,
                )
                logger.warning("missed_run week_of=%s", week.isoformat())
                reported.append(week)
            week += timedelta(days=7)
    return reported


def fail_interrupted_runs(session_factory: SessionFactory, *, now: datetime | None = None) -> int:
    """Mark ``running`` rows left by a killed process as ``failed``."""
    now = now or datetime.now(UTC)
    with _committed(session_factory) as session:
        stale = session.execute(
            select(
                pipeline_runs_table.c.id,
                pipeline_runs_table.c.job,
                pipeline_runs_table.c.week_of,
            ).where(pipeline_runs_table.c.status == "running")
        ).all()
        for row in stale:
            session.execute(
                pipeline_runs_table.update()
                .where(pipeline_runs_table.c.id == row.id)
                .values(status="failed", finished_at=now, detail="interrupted by restart")
            )
            record_alert(
                session,
                reason="pipeline_failed",
                job=row.job,
                week_of=row.week_of,
                detail=f"pipeline_runs id={row.id} interrupted by restart",
                now=now,
            )
    if stale:
        logger.warning("marked %d interrupted pipeline run(s) as failed", len(stale))
    return len(stale)


def run_startup_recovery(session_factory: SessionFactory) -> None:
    """Close out interrupted runs, report missed weeks, catch up this week."""
    for step in (fail_interrupted_runs, record_missed_runs, catch_up_weekly):
        try:
            step(session_factory)
        except Exception:
            logger.exception("startup recovery step %s failed", step.__name__)


def record_alert(
    session: Session,
    *,
    reason: str,
    job: str | None = None,
    week_of: date | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """Insert one ``alerts`` row (the caller owns the transaction)."""
    session.execute(
        alerts_table.insert().values(
            created_at=now or datetime.now(UTC),
            reason=reason,
            job=job,
            week_of=week_of,
            detail=detail,
        )
    )
    logger.warning(
        "alert reason=%s job=%s week_of=%s detail=%s",
        reason,
        job,
        week_of.isoformat() if week_of else "-",
        detail,
    )


def create_scheduler(
    session_factory: SessionFactory | None = None,
    *,
    scheduler: BackgroundScheduler | None = None,
) -> BackgroundScheduler:
    """Build and configure a :class:`BackgroundScheduler` with the two jobs.

    The scheduler is returned in an unstarted state — call ``.start()`` to
    begin firing. Passing ``session_factory`` lets tests substitute a
    factory bound to an in-memory engine; passing ``scheduler`` lets tests
    inspect the registered jobs without touching the process-wide default.
    """
    factory = session_factory or _default_session_factory
    sched = scheduler or BackgroundScheduler(timezone=UTC)

    sched.add_job(
        _make_ingest_callable(factory),
        trigger=CronTrigger(minute=0, timezone=UTC),
        id=INGEST_JOB_ID,
        name="hourly ingest",
        replace_existing=True,
        misfire_grace_time=INGEST_MISFIRE_GRACE_SECONDS,
        coalesce=True,
        max_instances=1,
    )
    sched.add_job(
        _make_weekly_callable(factory),
        trigger=CronTrigger(
            day_of_week=WEEKLY_PIPELINE_DAY_OF_WEEK,
            hour=WEEKLY_PIPELINE_HOUR,
            minute=WEEKLY_PIPELINE_MINUTE,
            timezone=WEEKLY_PIPELINE_TIMEZONE,
        ),
        id=WEEKLY_JOB_ID,
        name="weekly build/verify/publish",
        replace_existing=True,
        misfire_grace_time=WEEKLY_MISFIRE_GRACE_SECONDS,
        coalesce=True,
        max_instances=1,
    )
    return sched


def schedule_startup_recovery(
    sched: BackgroundScheduler, session_factory: SessionFactory | None = None
) -> None:
    """Queue :func:`run_startup_recovery` to run once, off the request path."""
    factory = session_factory or _default_session_factory
    sched.add_job(
        run_startup_recovery,
        args=(factory,),
        id=STARTUP_RECOVERY_JOB_ID,
        name="startup recovery / weekly catch-up",
        replace_existing=True,
        misfire_grace_time=None,
    )


def _make_ingest_callable(factory: SessionFactory) -> Callable[[], None]:
    def _job() -> None:
        run_ingest_job(factory)

    return _job


def _make_weekly_callable(factory: SessionFactory) -> Callable[[], None]:
    def _job() -> None:
        run_weekly_job(factory)

    return _job


def _tracked_run(
    session_factory: SessionFactory, job: str, body: Callable[[_RunRecord], None]
) -> _RunRecord:
    """Run ``body`` inside a ``pipeline_runs`` row; log and alert on failure."""
    started_at = datetime.now(UTC)
    with _committed(session_factory) as session:
        run_id = int(
            session.execute(
                pipeline_runs_table.insert()
                .values(job=job, started_at=started_at, status="running")
                .returning(pipeline_runs_table.c.id)
            ).scalar_one()
        )
    logger.info("pipeline_run start job=%s run_id=%d", job, run_id)

    run = _RunRecord()
    try:
        body(run)
    except Exception as exc:
        logger.exception("pipeline_run failed job=%s run_id=%d", job, run_id)
        run.status = "failed"
        run.detail = f"{type(exc).__name__}: {exc}"[:2000]

    finished_at = datetime.now(UTC)
    duration = finished_at - started_at
    try:
        with _committed(session_factory) as session:
            session.execute(
                pipeline_runs_table.update()
                .where(pipeline_runs_table.c.id == run_id)
                .values(
                    finished_at=finished_at,
                    status=run.status,
                    item_count=run.item_count,
                    week_of=run.week_of,
                    detail=run.detail,
                )
            )
            if run.status == "failed":
                record_alert(
                    session,
                    reason="pipeline_failed",
                    job=job,
                    week_of=run.week_of,
                    detail=f"pipeline_runs id={run_id}: {run.detail}",
                    now=finished_at,
                )
    except Exception:
        logger.exception("could not record pipeline_run id=%d", run_id)

    logger.info(
        "pipeline_run finish job=%s run_id=%d status=%s items=%s duration_s=%.1f",
        job,
        run_id,
        run.status,
        run.item_count,
        duration.total_seconds(),
    )
    if job == WEEKLY_JOB_ID and duration > WEEKLY_RUNTIME_BUDGET:
        logger.warning(
            "weekly_pipeline exceeded runtime budget: %.1f min > %d min",
            duration.total_seconds() / 60,
            int(WEEKLY_RUNTIME_BUDGET.total_seconds() // 60),
        )
    return run


def _issue_exists(session: Session, week_of: date) -> bool:
    return (
        session.execute(select(issues_table.c.id).where(issues_table.c.week_of == week_of)).first()
        is not None
    )


def _monday_of(dt: datetime) -> date:
    d = dt.astimezone(UTC).date()
    return d - timedelta(days=d.weekday())


def _ny_monday(dt: datetime) -> date:
    d = dt.astimezone(WEEKLY_PIPELINE_TIMEZONE).date()
    return d - timedelta(days=d.weekday())


def _default_session_factory() -> Session:
    return create_session_factory(get_engine())()


@contextmanager
def _committed(session_factory: SessionFactory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
