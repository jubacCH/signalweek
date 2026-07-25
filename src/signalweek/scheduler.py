"""Weekly APScheduler wiring for the ingest → digest pipeline.

The scheduler fires :func:`run_weekly_job` every Monday at 09:00 UTC.
Each run opens a fresh :class:`AsyncSession`, invokes an *ingest* step,
then an *digest* step, and commits their combined effect.

Ingest and digest are injected as callables so the job function stays
easy to trigger directly from tests without needing the real ingestion
adapters or a live network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from signalweek.db.models import Issue
from signalweek.digest import assemble_digest

WEEKLY_TRIGGER_DAY = "mon"
WEEKLY_TRIGGER_HOUR = 9
WEEKLY_TRIGGER_MINUTE = 0
WEEKLY_TRIGGER_TIMEZONE = "UTC"
JOB_ID = "signalweek.weekly"

IngestFn = Callable[[AsyncSession], Awaitable[Any]]
DigestFn = Callable[[AsyncSession, datetime], Awaitable[Issue]]
NotifyFn = Callable[[AsyncSession, Issue], Awaitable[Any]]


async def _default_ingest(session: AsyncSession) -> None:
    """Fallback ingest: dispatch to :mod:`signalweek.ingest.runner` if present.

    Imported lazily so the scheduler is usable on branches where the
    ingest package has not yet been merged. When the runner is missing
    we skip the ingest step rather than crashing the scheduled run.
    """

    try:
        from signalweek.ingest.runner import run_ingest  # type: ignore[import-not-found]
    except ImportError:
        return
    await run_ingest(session)


async def _default_digest(session: AsyncSession, now: datetime) -> Issue:
    return await assemble_digest(session, now=now)


async def _default_notify(session: AsyncSession, issue: Issue) -> list[str]:
    """Fallback notify: email the fresh issue to every active subscriber.

    Reads SMTP settings from the environment via
    :func:`signalweek.notify.email.smtp_config_from_env`; the default
    config is dry-run so no traffic leaves the box until it's opted in.
    """

    from signalweek.notify.email import (
        build_transport,
        send_issue_to_subscribers,
        smtp_config_from_env,
    )

    config = smtp_config_from_env()
    transport = build_transport(config)
    return await send_issue_to_subscribers(
        session,
        issue,
        transport=transport,
        from_address=config.from_address,
    )


async def run_weekly_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    ingest_fn: IngestFn = _default_ingest,
    digest_fn: DigestFn = _default_digest,
    notify_fn: NotifyFn = _default_notify,
) -> Issue:
    """Run the ingest → digest → notify pipeline once and return the issue.

    Opens a single :class:`AsyncSession` from ``session_factory``, runs
    ``ingest_fn`` to backfill any new signals, hands the same session to
    ``digest_fn`` to assemble the weekly issue, then invokes
    ``notify_fn`` so subscribers hear about it. ``now`` defaults to the
    current UTC instant and is normalized to a timezone-aware value
    before use.
    """

    ref = now if now is not None else datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)

    async with session_factory() as session:
        await ingest_fn(session)
        issue = await digest_fn(session, ref)
        await notify_fn(session, issue)
        return issue


def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ingest_fn: IngestFn = _default_ingest,
    digest_fn: DigestFn = _default_digest,
    notify_fn: NotifyFn = _default_notify,
) -> AsyncIOScheduler:
    """Return an :class:`AsyncIOScheduler` with the weekly job registered.

    The trigger fires every Monday at 09:00 UTC. The scheduler is
    returned unstarted so callers control the event loop lifecycle.
    """

    scheduler = AsyncIOScheduler(timezone=WEEKLY_TRIGGER_TIMEZONE)
    trigger = CronTrigger(
        day_of_week=WEEKLY_TRIGGER_DAY,
        hour=WEEKLY_TRIGGER_HOUR,
        minute=WEEKLY_TRIGGER_MINUTE,
        timezone=WEEKLY_TRIGGER_TIMEZONE,
    )
    scheduler.add_job(
        run_weekly_job,
        trigger=trigger,
        id=JOB_ID,
        kwargs={
            "session_factory": session_factory,
            "ingest_fn": ingest_fn,
            "digest_fn": digest_fn,
            "notify_fn": notify_fn,
        },
        replace_existing=True,
        misfire_grace_time=None,
    )
    return scheduler
