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
  clock change. Runs build → verify → publish for the current week.

Email delivery, per-subscriber send, and the LLM support check are all
deferred to a later stage. No ``send`` job is registered here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from signalweek.db.session import create_session_factory, get_engine
from signalweek.digest.builder import BuildResult, IssueAlreadyExistsError, build_issue
from signalweek.digest.verify import VerifyResult, verify_issue
from signalweek.ingest.cluster import cluster_raw_items
from signalweek.ingest.feeds import IngestRunResult, ingest_all_active

logger = logging.getLogger(__name__)

# The weekly pipeline fires Monday 09:00 in New York. The trigger reads its
# wall clock in this zone, so DST transitions do not shift the local hour.
WEEKLY_PIPELINE_TIMEZONE = ZoneInfo("America/New_York")
WEEKLY_PIPELINE_DAY_OF_WEEK = "mon"
WEEKLY_PIPELINE_HOUR = 9
WEEKLY_PIPELINE_MINUTE = 0

INGEST_JOB_ID = "ingest"
WEEKLY_JOB_ID = "weekly_pipeline"

# Callable that returns a fresh :class:`Session` on every invocation. A
# stock ``sessionmaker`` satisfies this contract, which lets tests inject
# a factory bound to an in-memory engine.
SessionFactory = Callable[[], Session]


@dataclass
class WeeklyPipelineResult:
    """Outcome of one weekly pipeline tick.

    ``verify`` is ``None`` when the issue was held (fewer than
    ``min_items`` items) — held issues stay in the database for an editor
    to inspect but are not verified or shipped.
    """

    build: BuildResult
    verify: VerifyResult | None = None


def run_ingest(session_factory: SessionFactory) -> IngestRunResult:
    """Fetch every active source, then rebuild clusters.

    The two writes are committed in separate transactions so a clustering
    failure does not roll back the raw_items that landed successfully.
    """
    with _committed(session_factory) as session:
        ingest_result = ingest_all_active(session)
    with _committed(session_factory) as session:
        cluster_result = cluster_raw_items(session)

    logger.info(
        "ingest_tick inserted=%d skipped=%d errors=%d clusters_created=%d clusters_matched=%d",
        ingest_result.total_inserted,
        ingest_result.total_skipped,
        len(ingest_result.errors),
        cluster_result.created,
        cluster_result.matched,
    )
    return ingest_result


def run_weekly_pipeline(
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
) -> WeeklyPipelineResult:
    """Build the current week's issue, verify its links, and log the outcome.

    A held issue is left untouched by the verify step. When an issue for the
    target week already exists the pipeline logs and returns without an
    error — the trigger is expected to fire idempotently.
    """
    now = now or datetime.now(UTC)

    try:
        with _committed(session_factory) as session:
            build_result = build_issue(session, now=now)
    except IssueAlreadyExistsError as exc:
        logger.info("weekly_pipeline skipped: %s", exc)
        raise

    verify_result: VerifyResult | None = None
    if build_result.status == "published":
        with _committed(session_factory) as session:
            verify_result = verify_issue(session, issue_id=build_result.issue_id)

    logger.info(
        "weekly_pipeline issue_id=%d week_of=%s status=%s items=%d verified=%s",
        build_result.issue_id,
        build_result.week_of.isoformat(),
        build_result.status,
        build_result.total_items,
        "yes" if verify_result is not None else "skipped",
    )
    return WeeklyPipelineResult(build=build_result, verify=verify_result)


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
    )
    return sched


def _make_ingest_callable(factory: SessionFactory) -> Callable[[], None]:
    def _job() -> None:
        try:
            run_ingest(factory)
        except Exception:
            logger.exception("ingest_tick failed")

    return _job


def _make_weekly_callable(factory: SessionFactory) -> Callable[[], None]:
    def _job() -> None:
        try:
            run_weekly_pipeline(factory)
        except IssueAlreadyExistsError:
            # Already logged inside run_weekly_pipeline.
            pass
        except Exception:
            logger.exception("weekly_pipeline failed")

    return _job


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
