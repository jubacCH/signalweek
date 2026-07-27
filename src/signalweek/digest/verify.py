"""Link liveness verifier: HEAD/GET every item URL and drop dead links.

Runs at publish time, after :func:`signalweek.digest.builder.build_issue`
has materialised the ``items`` rows for an issue but before subscribers see
them. For every item the verifier issues an HTTP ``HEAD`` request (falling
back to ``GET`` when the server refuses ``HEAD`` with ``405``/``501``) and
deletes the row from ``items`` when the final response is anything other
than ``200``. Network errors and timeouts also count as a drop — a link we
cannot reach is a link we should not ship.

The verifier logs the drop rate so an oncall can spot a bad build (e.g. a
paywall wave or a source that started 403'ing overnight). No third-party
service is contacted; probes go straight to the item's ``primary_url`` with
redirects followed.

Positions are left as-is when items are dropped. A gap in the ``position``
sequence is a harmless render-time detail; repacking would require an
extra write pass and is not this module's responsibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.sources import items_table

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_USER_AGENT = "signalweek-verify/0.1 (+https://signalweek.example)"

# Status codes that mean "this endpoint does not implement HEAD" — we retry
# the same URL with GET before deciding the link is dead.
_HEAD_FALLBACK_CODES = frozenset({405, 501})


@dataclass(frozen=True)
class DroppedItem:
    """One row removed from ``items`` because its primary_url did not verify."""

    item_id: int
    primary_url: str
    status_code: int | None
    reason: str


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of one :func:`verify_issue` run."""

    issue_id: int
    checked: int
    kept: int
    dropped: int
    dropped_items: tuple[DroppedItem, ...] = field(default_factory=tuple)

    @property
    def drop_rate(self) -> float:
        """Fraction of checked items removed, in ``[0.0, 1.0]``. ``0.0`` when
        the issue had no items to begin with."""
        if self.checked == 0:
            return 0.0
        return self.dropped / self.checked


def verify_issue(
    bind: Session | Connection,
    *,
    issue_id: int,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> VerifyResult:
    """Check every item in ``issue_id`` and delete rows whose link is dead.

    ``client`` may be supplied for tests (typically an
    :class:`httpx.MockTransport`-backed client); when omitted a short-lived
    client with sensible defaults is created for the run.
    """
    connection = _as_connection(bind)

    rows = connection.execute(
        select(items_table.c.id, items_table.c.primary_url)
        .where(items_table.c.issue_id == issue_id)
        .order_by(items_table.c.id)
    ).all()

    if not rows:
        logger.info(
            "verify_issue issue_id=%d checked=0 dropped=0 drop_rate=0.00",
            issue_id,
        )
        return VerifyResult(issue_id=issue_id, checked=0, kept=0, dropped=0)

    owned_client: httpx.Client | None = None
    if client is None:
        owned_client = httpx.Client(timeout=timeout)
        active_client = owned_client
    else:
        active_client = client

    dropped: list[DroppedItem] = []
    try:
        for row in rows:
            status, reason = _probe(
                active_client,
                url=row.primary_url,
                timeout=timeout,
                user_agent=user_agent,
            )
            if status != 200:
                dropped.append(
                    DroppedItem(
                        item_id=int(row.id),
                        primary_url=row.primary_url,
                        status_code=status,
                        reason=reason,
                    )
                )
    finally:
        if owned_client is not None:
            owned_client.close()

    if dropped:
        connection.execute(
            items_table.delete().where(items_table.c.id.in_([d.item_id for d in dropped]))
        )

    checked = len(rows)
    dropped_count = len(dropped)
    result = VerifyResult(
        issue_id=issue_id,
        checked=checked,
        kept=checked - dropped_count,
        dropped=dropped_count,
        dropped_items=tuple(dropped),
    )
    logger.info(
        "verify_issue issue_id=%d checked=%d dropped=%d drop_rate=%.2f",
        issue_id,
        result.checked,
        result.dropped,
        result.drop_rate,
    )
    return result


def _probe(
    client: httpx.Client,
    *,
    url: str,
    timeout: float,
    user_agent: str,
) -> tuple[int | None, str]:
    """Return ``(status_code, reason)`` for one liveness probe.

    ``status_code`` is ``None`` when the request never yielded a response
    (network error, DNS failure, timeout). ``reason`` is a short string
    suitable for logging — either ``"ok"``, ``"http_<code>"`` or the class
    name of the raised exception.
    """
    headers = {"User-Agent": user_agent}
    try:
        response = client.head(
            url,
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
        )
        if response.status_code in _HEAD_FALLBACK_CODES:
            response = client.get(
                url,
                headers=headers,
                follow_redirects=True,
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        return None, type(exc).__name__

    code = int(response.status_code)
    reason = "ok" if code == 200 else f"http_{code}"
    return code, reason


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
