"""Source health tracking and automatic pruning of dead/silent sources.

The registry stays fresh without any manual tending: the ingest layer
bumps per-source counters as it fetches, a probe pass keeps inactive
sources' fetch counters current, and :func:`prune_sources` applies the
rules that flip ``sources.active`` on and off — with every state change
recorded in ``source_health_events`` for audit.

Public entry points:

* :func:`record_fetch_success` / :func:`record_fetch_failure` — called by
  :mod:`signalweek.ingest.feeds` for every fetch attempt. Success resets
  ``consecutive_fetch_failures`` to zero and stamps ``last_fetch_ok_at``;
  failure increments the counter and stamps ``last_fetch_error_at``.
* :func:`record_items_seen` — bumps ``last_item_at`` when a source's
  fetch actually landed new raw_items.
* :func:`probe_inactive_sources` — pings every inactive source once so the
  next prune pass has fresh evidence of whether it has recovered. Only
  fetches; does not parse or persist raw_items.
* :func:`prune_sources` — the periodic maintenance step. Deactivates
  active sources whose feeds have been unreachable for
  ``max_consecutive_failures`` runs or that have produced no items for
  ``silent_weeks`` weeks; reactivates inactive sources whose fetch
  counters show they are healthy again. Returns the list of events it
  wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.ingest.feeds import FetchError, fetch_feed
from signalweek.sources import source_health_events_table, sources_table

# A source that has failed to fetch this many times in a row is considered
# dead and gets deactivated. Tunable at the call site.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

# A source that has produced no items for this many weeks — while being
# fetchable — is considered silent and gets deactivated.
DEFAULT_SILENT_WEEKS = 8

# Machine-readable reason tags written to ``source_health_events.reason``.
REASON_FETCH_FAILURES = "fetch_failures"
REASON_SILENT = "silent"
REASON_RECOVERED = "recovered"

# Actions written to ``source_health_events.action``.
ACTION_ACTIVATED = "activated"
ACTION_DEACTIVATED = "deactivated"


@dataclass(frozen=True)
class HealthEvent:
    """One row appended to ``source_health_events``."""

    source_id: int
    url: str
    action: str
    reason: str
    at: datetime


@dataclass
class PruneResult:
    """Outcome of one :func:`prune_sources` run."""

    deactivated: list[HealthEvent] = field(default_factory=list)
    reactivated: list[HealthEvent] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.deactivated) + len(self.reactivated)


@dataclass
class ProbeResult:
    """Outcome of one :func:`probe_inactive_sources` run."""

    probed: int = 0
    succeeded: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Health counters — called by the ingest layer on every fetch attempt.
# ---------------------------------------------------------------------------


def record_fetch_success(
    bind: Session | Connection,
    *,
    source_id: int,
    now: datetime,
) -> None:
    """Mark a fetch of ``source_id`` as successful.

    Resets the consecutive-failures counter so a single good fetch clears
    prior transient errors, and stamps ``last_fetch_ok_at``.
    """
    connection = _as_connection(bind)
    connection.execute(
        sources_table.update()
        .where(sources_table.c.id == int(source_id))
        .values(consecutive_fetch_failures=0, last_fetch_ok_at=_ensure_aware(now))
    )


def record_fetch_failure(
    bind: Session | Connection,
    *,
    source_id: int,
    now: datetime,
) -> None:
    """Mark a fetch of ``source_id`` as failed.

    Increments ``consecutive_fetch_failures`` in place and stamps
    ``last_fetch_error_at``. Uses ``COALESCE(counter, 0) + 1`` so a NULL
    (which shouldn't happen given the NOT NULL default, but might in
    legacy rows) still moves forward.
    """
    connection = _as_connection(bind)
    connection.execute(
        sources_table.update()
        .where(sources_table.c.id == int(source_id))
        .values(
            consecutive_fetch_failures=sources_table.c.consecutive_fetch_failures + 1,
            last_fetch_error_at=_ensure_aware(now),
        )
    )


def record_items_seen(
    bind: Session | Connection,
    *,
    source_id: int,
    now: datetime,
) -> None:
    """Stamp ``last_item_at`` for ``source_id``.

    Called by the ingest layer whenever a fetch actually landed new
    ``raw_items`` — this is the anchor for the silent-source rule.
    """
    connection = _as_connection(bind)
    connection.execute(
        sources_table.update()
        .where(sources_table.c.id == int(source_id))
        .values(last_item_at=_ensure_aware(now))
    )


# ---------------------------------------------------------------------------
# Probe — keep inactive sources' fetch counters fresh so prune can spot
# recoveries. Does not parse or insert; only records success/failure.
# ---------------------------------------------------------------------------


def probe_inactive_sources(
    bind: Session | Connection,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> ProbeResult:
    """Try fetching every inactive source once and record the outcome.

    A successful probe resets the consecutive-failures counter and
    stamps ``last_fetch_ok_at``, so the next :func:`prune_sources` call
    can promote the source back to active. A failing probe increments the
    counter.
    """
    connection = _as_connection(bind)
    stamp = _resolve_now(now)

    rows = connection.execute(
        select(sources_table.c.id, sources_table.c.url)
        .where(sources_table.c.active.is_(False))
        .order_by(sources_table.c.id)
    ).all()

    result = ProbeResult()
    for row in rows:
        result.probed += 1
        try:
            fetch_feed(row.url, client=client)
        except FetchError:
            record_fetch_failure(connection, source_id=int(row.id), now=stamp)
            result.failed += 1
        else:
            record_fetch_success(connection, source_id=int(row.id), now=stamp)
            result.succeeded += 1
    return result


# ---------------------------------------------------------------------------
# Prune — apply the deactivation / reactivation rules and log events.
# ---------------------------------------------------------------------------


def prune_sources(
    bind: Session | Connection,
    *,
    now: datetime | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    silent_weeks: int = DEFAULT_SILENT_WEEKS,
) -> PruneResult:
    """Deactivate dead/silent sources and reactivate recovered ones.

    Deactivation rules (applied to currently-active rows):

    * ``fetch_failures`` — ``consecutive_fetch_failures`` has reached
      ``max_consecutive_failures``.
    * ``silent`` — the source has been observable
      (``last_fetch_ok_at`` set at least ``silent_weeks`` ago) but has
      produced no ``raw_items`` in that window
      (``last_item_at`` is NULL or older than the cutoff).

    Reactivation rule (applied to currently-inactive rows that have been
    deactivated by this module — ``deactivated_at`` is set):

    * ``recovered`` — the source is fetching cleanly again
      (``consecutive_fetch_failures == 0`` and ``last_fetch_ok_at`` is set)
      and, if it had been deactivated for silence, has landed a fresh
      item within the ``silent_weeks`` window.

    Every state change is inserted into ``source_health_events`` and
    returned in the :class:`PruneResult`.
    """
    if max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be >= 1")
    if silent_weeks < 1:
        raise ValueError("silent_weeks must be >= 1")

    connection = _as_connection(bind)
    stamp = _resolve_now(now)
    silent_cutoff = stamp - timedelta(weeks=silent_weeks)

    result = PruneResult()

    rows = connection.execute(
        select(
            sources_table.c.id,
            sources_table.c.url,
            sources_table.c.active,
            sources_table.c.consecutive_fetch_failures,
            sources_table.c.last_fetch_ok_at,
            sources_table.c.last_item_at,
            sources_table.c.deactivated_at,
            sources_table.c.deactivation_reason,
        ).order_by(sources_table.c.id)
    ).all()

    for row in rows:
        failures = int(row.consecutive_fetch_failures or 0)
        last_ok = _as_aware(row.last_fetch_ok_at)
        last_item = _as_aware(row.last_item_at)
        prior_reason = row.deactivation_reason

        if bool(row.active):
            reason = _pick_deactivation_reason(
                failures=failures,
                last_ok=last_ok,
                last_item=last_item,
                silent_cutoff=silent_cutoff,
                max_consecutive_failures=max_consecutive_failures,
            )
            if reason is not None:
                _deactivate(connection, source_id=int(row.id), reason=reason, at=stamp)
                result.deactivated.append(
                    HealthEvent(
                        source_id=int(row.id),
                        url=row.url,
                        action=ACTION_DEACTIVATED,
                        reason=reason,
                        at=stamp,
                    )
                )
            continue

        # Currently inactive — only touch rows this module deactivated, to
        # avoid stomping on operator-disabled sources.
        if row.deactivated_at is None:
            continue

        if _is_recovered(
            failures=failures,
            last_ok=last_ok,
            last_item=last_item,
            silent_cutoff=silent_cutoff,
            prior_reason=prior_reason,
        ):
            _reactivate(connection, source_id=int(row.id), at=stamp)
            result.reactivated.append(
                HealthEvent(
                    source_id=int(row.id),
                    url=row.url,
                    action=ACTION_ACTIVATED,
                    reason=REASON_RECOVERED,
                    at=stamp,
                )
            )

    return result


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------


def _pick_deactivation_reason(
    *,
    failures: int,
    last_ok: datetime | None,
    last_item: datetime | None,
    silent_cutoff: datetime,
    max_consecutive_failures: int,
) -> str | None:
    if failures >= max_consecutive_failures:
        return REASON_FETCH_FAILURES
    # A source that has never been reached is handled by the failures
    # rule above; the silence rule only fires once we've had a chance to
    # observe it for at least ``silent_weeks`` and still see nothing.
    if last_ok is None or last_ok > silent_cutoff:
        return None
    if last_item is None or last_item <= silent_cutoff:
        return REASON_SILENT
    return None


def _is_recovered(
    *,
    failures: int,
    last_ok: datetime | None,
    last_item: datetime | None,
    silent_cutoff: datetime,
    prior_reason: str | None,
) -> bool:
    # Must be currently fetchable.
    if failures != 0 or last_ok is None:
        return False
    # A silence-deactivated source must show fresh item flow before we
    # promote it back — a bare fetch is not evidence of new content.
    if prior_reason == REASON_SILENT:
        return last_item is not None and last_item > silent_cutoff
    return True


def _deactivate(connection: Connection, *, source_id: int, reason: str, at: datetime) -> None:
    connection.execute(
        sources_table.update()
        .where(sources_table.c.id == source_id)
        .values(active=False, deactivated_at=at, deactivation_reason=reason)
    )
    connection.execute(
        source_health_events_table.insert().values(
            source_id=source_id,
            at=at,
            action=ACTION_DEACTIVATED,
            reason=reason,
        )
    )


def _reactivate(connection: Connection, *, source_id: int, at: datetime) -> None:
    connection.execute(
        sources_table.update()
        .where(sources_table.c.id == source_id)
        .values(
            active=True,
            deactivated_at=None,
            deactivation_reason=None,
            consecutive_fetch_failures=0,
        )
    )
    connection.execute(
        source_health_events_table.insert().values(
            source_id=source_id,
            at=at,
            action=ACTION_ACTIVATED,
            reason=REASON_RECOVERED,
        )
    )


# ---------------------------------------------------------------------------
# Time / connection utilities
# ---------------------------------------------------------------------------


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return _ensure_aware(now)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_aware(value)


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
