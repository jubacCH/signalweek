"""Outbound notification adapters (email, etc.)."""

from __future__ import annotations

from signalweek.notify.email import (
    DEFAULT_FROM_ADDRESS,
    DEFAULT_SMTP_PORT,
    DryRunTransport,
    EmailMessage,
    SmtpConfig,
    SmtpTransport,
    Transport,
    build_issue_message,
    build_transport,
    send_issue_to_subscribers,
    smtp_config_from_env,
)

__all__ = [
    "DEFAULT_FROM_ADDRESS",
    "DEFAULT_SMTP_PORT",
    "DryRunTransport",
    "EmailMessage",
    "SmtpConfig",
    "SmtpTransport",
    "Transport",
    "build_issue_message",
    "build_transport",
    "send_issue_to_subscribers",
    "smtp_config_from_env",
]
