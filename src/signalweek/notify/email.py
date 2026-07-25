"""SMTP delivery adapter for the weekly issue.

Delivery goes through a small :class:`Transport` protocol so tests can
swap the wire for a captured-message double. The default live transport
wraps stdlib :mod:`smtplib` in :func:`asyncio.to_thread` (aiosmtplib
would be a drop-in replacement here, but stdlib keeps the runtime
dependency-free). The default mode is *dry-run*: misconfiguration
cannot accidentally spam subscribers — an explicit
``SIGNALWEEK_SMTP_DRY_RUN=false`` is required to talk to a real server.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as _StdlibEmailMessage
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalweek.db.models import (
    SUBSCRIBER_STATUS_ACTIVE,
    Issue,
    Subscriber,
)

DEFAULT_SMTP_HOST = "localhost"
DEFAULT_SMTP_PORT = 25
DEFAULT_FROM_ADDRESS = "signalweek@localhost"

ENV_HOST = "SIGNALWEEK_SMTP_HOST"
ENV_PORT = "SIGNALWEEK_SMTP_PORT"
ENV_USERNAME = "SIGNALWEEK_SMTP_USERNAME"
ENV_PASSWORD = "SIGNALWEEK_SMTP_PASSWORD"
ENV_FROM = "SIGNALWEEK_SMTP_FROM"
ENV_USE_TLS = "SIGNALWEEK_SMTP_USE_TLS"
ENV_DRY_RUN = "SIGNALWEEK_SMTP_DRY_RUN"


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Connection settings for the SMTP adapter."""

    host: str = DEFAULT_SMTP_HOST
    port: int = DEFAULT_SMTP_PORT
    username: str | None = None
    password: str | None = None
    from_address: str = DEFAULT_FROM_ADDRESS
    use_tls: bool = False
    dry_run: bool = True


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A single outbound email destined for one subscriber."""

    to: str
    subject: str
    body: str
    from_address: str


@runtime_checkable
class Transport(Protocol):
    """Anything that can asynchronously deliver an :class:`EmailMessage`."""

    async def send(self, message: EmailMessage) -> None: ...


class DryRunTransport:
    """Transport that captures messages instead of talking to a server."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class SmtpTransport:
    """Live SMTP transport backed by stdlib :mod:`smtplib`."""

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._sync_send, message)

    def _sync_send(self, message: EmailMessage) -> None:
        payload = _StdlibEmailMessage()
        payload["From"] = message.from_address
        payload["To"] = message.to
        payload["Subject"] = message.subject
        payload.set_content(message.body)

        cfg = self._config
        client_cls = smtplib.SMTP_SSL if cfg.use_tls else smtplib.SMTP
        with client_cls(cfg.host, cfg.port) as client:
            if cfg.username and cfg.password:
                client.login(cfg.username, cfg.password)
            client.send_message(payload)


def build_transport(config: SmtpConfig) -> Transport:
    """Return the concrete transport implied by ``config.dry_run``."""

    if config.dry_run:
        return DryRunTransport()
    return SmtpTransport(config)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return _parse_bool(raw)


def _str_env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _optional_env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return raw


def smtp_config_from_env() -> SmtpConfig:
    """Build an :class:`SmtpConfig` from ``SIGNALWEEK_SMTP_*`` env vars.

    Missing variables fall back to safe defaults; in particular
    ``dry_run`` defaults to ``True`` so a bare ``signalweek schedule``
    process never sends real email.
    """

    return SmtpConfig(
        host=_str_env(ENV_HOST, DEFAULT_SMTP_HOST),
        port=int(_str_env(ENV_PORT, str(DEFAULT_SMTP_PORT))),
        username=_optional_env(ENV_USERNAME),
        password=_optional_env(ENV_PASSWORD),
        from_address=_str_env(ENV_FROM, DEFAULT_FROM_ADDRESS),
        use_tls=_bool_env(ENV_USE_TLS, False),
        dry_run=_bool_env(ENV_DRY_RUN, True),
    )


def build_issue_message(
    issue: Issue, subscriber: Subscriber, *, from_address: str
) -> EmailMessage:
    """Render one outbound :class:`EmailMessage` for ``subscriber``."""

    return EmailMessage(
        to=subscriber.email,
        subject=issue.title,
        body=issue.body_markdown,
        from_address=from_address,
    )


async def send_issue_to_subscribers(
    session: AsyncSession,
    issue: Issue,
    *,
    transport: Transport,
    from_address: str,
) -> list[str]:
    """Send ``issue`` to every active subscriber; return delivered addresses.

    Subscribers still in the ``pending`` (unconfirmed) state are skipped.
    The returned list preserves the order in which messages were handed
    to ``transport``.
    """

    stmt = (
        select(Subscriber)
        .where(Subscriber.status == SUBSCRIBER_STATUS_ACTIVE)
        .order_by(Subscriber.id)
    )
    subscribers = list((await session.execute(stmt)).scalars().all())

    delivered: list[str] = []
    for subscriber in subscribers:
        message = build_issue_message(issue, subscriber, from_address=from_address)
        await transport.send(message)
        delivered.append(subscriber.email)
    return delivered
