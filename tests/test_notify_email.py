"""Tests for the SMTP delivery adapter and its scheduler hook."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from signalweek.db import (
    SUBSCRIBER_STATUS_ACTIVE,
    SUBSCRIBER_STATUS_PENDING,
    Issue,
    Subscriber,
    create_session_factory,
)
from signalweek.notify.email import (
    DEFAULT_FROM_ADDRESS,
    DEFAULT_SMTP_HOST,
    DEFAULT_SMTP_PORT,
    ENV_DRY_RUN,
    ENV_FROM,
    ENV_HOST,
    ENV_PASSWORD,
    ENV_PORT,
    ENV_USE_TLS,
    ENV_USERNAME,
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
from signalweek.scheduler import JOB_ID, build_scheduler, run_weekly_job

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)  # Saturday, ISO 2026-W30
WEEK_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

_ENV_VARS = (
    ENV_HOST,
    ENV_PORT,
    ENV_USERNAME,
    ENV_PASSWORD,
    ENV_FROM,
    ENV_USE_TLS,
    ENV_DRY_RUN,
)


class CapturingTransport:
    """Test double: records every :class:`EmailMessage` it's handed."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


def _clear_smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _sample_issue() -> Issue:
    return Issue(
        number=202630,
        title="SignalWeek 2026-W30",
        body_markdown="# SignalWeek 2026-W30\n\nGreat week.\n",
        published_at=WEEK_START,
    )


class TestSmtpConfigDefaults:
    def test_dry_run_is_the_default(self) -> None:
        assert SmtpConfig().dry_run is True

    def test_default_host_and_port(self) -> None:
        cfg = SmtpConfig()
        assert cfg.host == DEFAULT_SMTP_HOST
        assert cfg.port == DEFAULT_SMTP_PORT
        assert cfg.from_address == DEFAULT_FROM_ADDRESS
        assert cfg.username is None
        assert cfg.password is None
        assert cfg.use_tls is False


class TestSmtpConfigFromEnv:
    def test_defaults_to_dry_run_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_smtp_env(monkeypatch)
        cfg = smtp_config_from_env()
        assert cfg.dry_run is True
        assert cfg.host == DEFAULT_SMTP_HOST
        assert cfg.port == DEFAULT_SMTP_PORT
        assert cfg.from_address == DEFAULT_FROM_ADDRESS
        assert cfg.username is None
        assert cfg.password is None
        assert cfg.use_tls is False

    def test_reads_all_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_smtp_env(monkeypatch)
        monkeypatch.setenv(ENV_HOST, "mail.example.com")
        monkeypatch.setenv(ENV_PORT, "587")
        monkeypatch.setenv(ENV_USERNAME, "alice")
        monkeypatch.setenv(ENV_PASSWORD, "s3cret")
        monkeypatch.setenv(ENV_FROM, "issues@example.com")
        monkeypatch.setenv(ENV_USE_TLS, "true")
        monkeypatch.setenv(ENV_DRY_RUN, "false")

        cfg = smtp_config_from_env()
        assert cfg.host == "mail.example.com"
        assert cfg.port == 587
        assert cfg.username == "alice"
        assert cfg.password == "s3cret"
        assert cfg.from_address == "issues@example.com"
        assert cfg.use_tls is True
        assert cfg.dry_run is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_boolean_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        _clear_smtp_env(monkeypatch)
        monkeypatch.setenv(ENV_DRY_RUN, raw)
        assert smtp_config_from_env().dry_run is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
    def test_boolean_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        _clear_smtp_env(monkeypatch)
        monkeypatch.setenv(ENV_DRY_RUN, raw)
        assert smtp_config_from_env().dry_run is False

    def test_empty_env_values_fall_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_smtp_env(monkeypatch)
        monkeypatch.setenv(ENV_HOST, "")
        monkeypatch.setenv(ENV_USERNAME, "")
        cfg = smtp_config_from_env()
        assert cfg.host == DEFAULT_SMTP_HOST
        assert cfg.username is None


class TestBuildTransport:
    def test_dry_run_returns_capturing_transport(self) -> None:
        transport = build_transport(SmtpConfig(dry_run=True))
        assert isinstance(transport, DryRunTransport)

    def test_live_returns_smtp_transport(self) -> None:
        transport = build_transport(SmtpConfig(dry_run=False))
        assert isinstance(transport, SmtpTransport)


class TestTransportProtocol:
    def test_dry_run_transport_satisfies_protocol(self) -> None:
        assert isinstance(DryRunTransport(), Transport)

    def test_capturing_double_satisfies_protocol(self) -> None:
        assert isinstance(CapturingTransport(), Transport)


class TestDryRunTransport:
    async def test_records_every_message(self) -> None:
        transport = DryRunTransport()
        msg = EmailMessage(
            to="a@example.com",
            subject="hi",
            body="body",
            from_address="from@example.com",
        )
        await transport.send(msg)
        assert transport.sent == [msg]


class TestBuildIssueMessage:
    def test_uses_issue_title_and_body(self) -> None:
        issue = _sample_issue()
        subscriber = Subscriber(
            email="reader@example.com",
            confirmation_token="tok",
            status=SUBSCRIBER_STATUS_ACTIVE,
        )
        message = build_issue_message(
            issue, subscriber, from_address="issues@example.com"
        )
        assert message.to == "reader@example.com"
        assert message.subject == issue.title
        assert message.body == issue.body_markdown
        assert message.from_address == "issues@example.com"


class TestSendIssueToSubscribers:
    async def test_sends_only_to_active_subscribers(
        self, session: AsyncSession
    ) -> None:
        issue = _sample_issue()
        session.add(issue)
        session.add(
            Subscriber(
                email="alice@example.com",
                confirmation_token="t-alice",
                status=SUBSCRIBER_STATUS_ACTIVE,
                confirmed_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        session.add(
            Subscriber(
                email="bob@example.com",
                confirmation_token="t-bob",
                status=SUBSCRIBER_STATUS_PENDING,
            )
        )
        session.add(
            Subscriber(
                email="carol@example.com",
                confirmation_token="t-carol",
                status=SUBSCRIBER_STATUS_ACTIVE,
                confirmed_at=datetime(2026, 7, 2, tzinfo=UTC),
            )
        )
        await session.flush()

        transport = CapturingTransport()
        delivered = await send_issue_to_subscribers(
            session,
            issue,
            transport=transport,
            from_address="issues@example.com",
        )

        assert delivered == ["alice@example.com", "carol@example.com"]
        recipients = [m.to for m in transport.sent]
        assert recipients == ["alice@example.com", "carol@example.com"]
        assert all(m.subject == issue.title for m in transport.sent)
        assert all(m.body == issue.body_markdown for m in transport.sent)
        assert all(m.from_address == "issues@example.com" for m in transport.sent)

    async def test_no_active_subscribers_no_sends(
        self, session: AsyncSession
    ) -> None:
        issue = _sample_issue()
        session.add(issue)
        session.add(
            Subscriber(
                email="pending@example.com",
                confirmation_token="tok",
                status=SUBSCRIBER_STATUS_PENDING,
            )
        )
        await session.flush()

        transport = CapturingTransport()
        delivered = await send_issue_to_subscribers(
            session,
            issue,
            transport=transport,
            from_address="issues@example.com",
        )

        assert delivered == []
        assert transport.sent == []

    async def test_works_with_dry_run_transport(
        self, session: AsyncSession
    ) -> None:
        issue = _sample_issue()
        session.add(issue)
        session.add(
            Subscriber(
                email="alice@example.com",
                confirmation_token="tok",
                status=SUBSCRIBER_STATUS_ACTIVE,
                confirmed_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        await session.flush()

        transport = DryRunTransport()
        delivered = await send_issue_to_subscribers(
            session,
            issue,
            transport=transport,
            from_address="issues@example.com",
        )
        assert delivered == ["alice@example.com"]
        assert len(transport.sent) == 1
        assert transport.sent[0].to == "alice@example.com"


class TestSchedulerNotifyHook:
    async def test_notify_fn_runs_after_digest_with_the_new_issue(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)
        call_order: list[str] = []
        seen_issue: dict[str, Issue] = {}

        async def fake_ingest(session: AsyncSession) -> None:
            call_order.append("ingest")

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            call_order.append("digest")
            issue = Issue(
                number=202630,
                title="SignalWeek 2026-W30",
                body_markdown="body",
                published_at=now,
            )
            session.add(issue)
            await session.commit()
            return issue

        async def fake_notify(session: AsyncSession, issue: Issue) -> None:
            call_order.append("notify")
            seen_issue["value"] = issue

        result = await run_weekly_job(
            factory,
            now=NOW,
            ingest_fn=fake_ingest,
            digest_fn=fake_digest,
            notify_fn=fake_notify,
        )
        assert call_order == ["ingest", "digest", "notify"]
        assert seen_issue["value"] is result

    async def test_default_notify_emails_active_subscribers_via_dry_run(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default hook builds a dry-run transport and delivers to actives."""

        _clear_smtp_env(monkeypatch)
        monkeypatch.setenv(ENV_FROM, "issues@example.com")

        captured: dict[str, Any] = {}

        original_build = build_transport

        def spy_build_transport(config: SmtpConfig) -> Transport:
            transport = original_build(config)
            captured["transport"] = transport
            captured["config"] = config
            return transport

        monkeypatch.setattr(
            "signalweek.notify.email.build_transport", spy_build_transport
        )

        factory = create_session_factory(engine)

        async def seed_ingest(session: AsyncSession) -> None:
            session.add(
                Subscriber(
                    email="reader@example.com",
                    confirmation_token="tok",
                    status=SUBSCRIBER_STATUS_ACTIVE,
                    confirmed_at=datetime(2026, 7, 1, tzinfo=UTC),
                )
            )
            session.add(
                Subscriber(
                    email="waiting@example.com",
                    confirmation_token="tok2",
                    status=SUBSCRIBER_STATUS_PENDING,
                )
            )
            await session.flush()

        async def fake_digest(session: AsyncSession, now: datetime) -> Issue:
            issue = Issue(
                number=202630,
                title="SignalWeek 2026-W30",
                body_markdown="body",
                published_at=now,
            )
            session.add(issue)
            await session.commit()
            return issue

        issue = await run_weekly_job(
            factory,
            now=NOW,
            ingest_fn=seed_ingest,
            digest_fn=fake_digest,
        )

        transport = captured["transport"]
        config = captured["config"]
        assert isinstance(transport, DryRunTransport)
        assert config.dry_run is True
        assert config.from_address == "issues@example.com"
        assert [m.to for m in transport.sent] == ["reader@example.com"]
        assert transport.sent[0].subject == issue.title
        assert transport.sent[0].body == issue.body_markdown
        assert transport.sent[0].from_address == "issues@example.com"

    def test_build_scheduler_registers_notify_fn_in_job_kwargs(
        self, engine: AsyncEngine
    ) -> None:
        factory = create_session_factory(engine)

        async def custom_notify(session: AsyncSession, issue: Issue) -> None:
            return None

        scheduler = build_scheduler(factory, notify_fn=custom_notify)
        job = scheduler.get_job(JOB_ID)
        assert job is not None
        assert job.kwargs["notify_fn"] is custom_notify

    async def test_scheduler_job_wires_notify_end_to_end(
        self, engine: AsyncEngine
    ) -> None:
        """Firing the registered job invokes the injected notify function."""

        factory = create_session_factory(engine)
        received: list[tuple[str, Issue]] = []

        async def seed_ingest(session: AsyncSession) -> None:
            session.add(
                Subscriber(
                    email="reader@example.com",
                    confirmation_token="tok",
                    status=SUBSCRIBER_STATUS_ACTIVE,
                    confirmed_at=datetime(2026, 7, 1, tzinfo=UTC),
                )
            )
            session.add(
                Subscriber(
                    email="hn@example.com",
                    confirmation_token="tok2",
                    status=SUBSCRIBER_STATUS_ACTIVE,
                    confirmed_at=datetime(2026, 7, 1, tzinfo=UTC),
                )
            )
            from signalweek.db.models import SignalItem

            session.add(
                SignalItem(
                    url="https://example.test/one",
                    title="Distributed consensus made simple",
                    source="Hacker News",
                    published_at=WEEK_START + timedelta(hours=3),
                )
            )
            await session.flush()

        transport = CapturingTransport()

        async def capture_notify(session: AsyncSession, issue: Issue) -> None:
            delivered = await send_issue_to_subscribers(
                session,
                issue,
                transport=transport,
                from_address="issues@example.com",
            )
            for email in delivered:
                received.append((email, issue))

        scheduler = build_scheduler(
            factory, ingest_fn=seed_ingest, notify_fn=capture_notify
        )
        job = scheduler.get_job(JOB_ID)
        assert job is not None

        issue = await job.func(
            session_factory=factory,
            now=NOW,
            ingest_fn=seed_ingest,
            digest_fn=job.kwargs["digest_fn"],
            notify_fn=job.kwargs["notify_fn"],
        )
        assert [row[0] for row in received] == ["reader@example.com", "hn@example.com"]
        assert all(row[1] is issue for row in received)
        assert [m.to for m in transport.sent] == [
            "reader@example.com",
            "hn@example.com",
        ]
