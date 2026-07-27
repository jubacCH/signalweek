"""Tests for :mod:`signalweek.cli`.

The CLI is exercised through its ``main`` entry point with an injected
in-memory engine so tests never touch the filesystem or a real database.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from signalweek.cli import (
    EXIT_CONFLICT,
    EXIT_NOT_FOUND,
    EXIT_OK,
    main,
)
from signalweek.db.session import create_db_engine
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _run(engine: Engine, argv: list[str], *, now: datetime | None = None) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = main(argv, engine=engine, stdout=out, stderr=err, now=now or NOW)
    return code, out.getvalue(), err.getvalue()


def _seed_publishable_clusters(conn: Connection, *, now: datetime = NOW) -> None:
    """Seed 11 recent clusters spread across the five sections so a build publishes."""
    result = conn.execute(
        sources_table.insert()
        .values(url="https://openai.com/blog/rss.xml", kind="rss", category_hint="models")
        .returning(sources_table.c.id)
    )
    source_id = int(result.scalar_one())

    seeds = [
        ("models", "OpenAI unveils GPT-5", "https://openai.com/blog/gpt-5"),
        ("models", "Anthropic releases Claude 5", "https://openai.com/blog/claude-5"),
        ("models", "Google launches Gemini 3", "https://openai.com/blog/gemini-3"),
        ("funding", "Anthropic raises Series F", "https://openai.com/blog/anthropic-round"),
        ("funding", "Startup raises Series C", "https://openai.com/blog/startup-round"),
        ("lawsuits_policy", "Court rules against AI vendor", "https://openai.com/blog/court"),
        ("lawsuits_policy", "Executive order signed on AI", "https://openai.com/blog/eo"),
        ("research", "New preprint proposes benchmark", "https://openai.com/blog/preprint"),
        ("research", "Researchers report SOTA", "https://openai.com/blog/sota"),
        ("industry_moves", "Google hires new AI CEO", "https://openai.com/blog/hire"),
        ("industry_moves", "Meta announces AI layoffs", "https://openai.com/blog/layoffs"),
    ]
    for category, headline, url in seeds:
        conn.execute(
            raw_items_table.insert().values(
                source_id=source_id,
                url=url,
                canonical_url=url,
                title=headline,
                body=f"{headline}. Extended lede content.",
                fetched_at=now,
                first_seen_at=now,
            )
        )
        conn.execute(
            clusters_table.insert().values(
                primary_url=url,
                canonical_headline=headline,
                category=category,
            )
        )


# ---------------------------------------------------------------------------
# sources add / list / disable
# ---------------------------------------------------------------------------


class TestSourcesAdd:
    def test_add_inserts_new_row(self, engine: Engine) -> None:
        code, out, _ = _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://example.com/feed.xml",
                "--kind",
                "rss",
                "--category",
                "models",
                "--name",
                "Example",
            ],
        )
        assert code == EXIT_OK
        assert "added source https://example.com/feed.xml" in out

        with engine.connect() as conn:
            rows = conn.execute(select(sources_table)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.url == "https://example.com/feed.xml"
        assert row.kind == "rss"
        assert row.category_hint == "models"
        assert bool(row.active) is True

    def test_add_upserts_existing_row(self, engine: Engine) -> None:
        _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://example.com/feed.xml",
                "--kind",
                "rss",
                "--category",
                "models",
            ],
        )
        # Re-add with a different category: should update, not error.
        code, out, _ = _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://example.com/feed.xml",
                "--kind",
                "atom",
                "--category",
                "research",
            ],
        )
        assert code == EXIT_OK
        assert "updated source https://example.com/feed.xml" in out

        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).one()
        assert row.kind == "atom"
        assert row.category_hint == "research"

    def test_add_re_enables_a_disabled_source(self, engine: Engine) -> None:
        _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://example.com/feed.xml",
                "--kind",
                "rss",
                "--category",
                "models",
            ],
        )
        _run(engine, ["sources", "disable", "--url", "https://example.com/feed.xml"])
        code, out, _ = _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://example.com/feed.xml",
                "--kind",
                "rss",
                "--category",
                "models",
            ],
        )
        assert code == EXIT_OK
        assert "updated source" in out

        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).one()
        assert bool(row.active) is True

    def test_add_rejects_unknown_kind(self, engine: Engine) -> None:
        with pytest.raises(SystemExit):
            _run(
                engine,
                [
                    "sources",
                    "add",
                    "--url",
                    "https://example.com/feed.xml",
                    "--kind",
                    "podcast",
                    "--category",
                    "models",
                ],
            )

    def test_add_rejects_unknown_category(self, engine: Engine) -> None:
        with pytest.raises(SystemExit):
            _run(
                engine,
                [
                    "sources",
                    "add",
                    "--url",
                    "https://example.com/feed.xml",
                    "--kind",
                    "rss",
                    "--category",
                    "sports",
                ],
            )


class TestSourcesList:
    def test_list_empty_registry(self, engine: Engine) -> None:
        code, out, _ = _run(engine, ["sources", "list"])
        assert code == EXIT_OK
        assert "no sources registered" in out

    def test_list_prints_active_and_inactive_sources(self, engine: Engine) -> None:
        _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://a.example/feed",
                "--kind",
                "rss",
                "--category",
                "models",
            ],
        )
        _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://b.example/feed",
                "--kind",
                "atom",
                "--category",
                "research",
            ],
        )
        _run(engine, ["sources", "disable", "--url", "https://b.example/feed"])

        code, out, _ = _run(engine, ["sources", "list"])
        assert code == EXIT_OK
        # Both sources appear.
        assert "https://a.example/feed" in out
        assert "https://b.example/feed" in out
        # State flags render.
        assert "active" in out
        assert "inactive" in out


class TestSourcesDisable:
    def test_disable_flips_active_to_false(self, engine: Engine) -> None:
        _run(
            engine,
            [
                "sources",
                "add",
                "--url",
                "https://example.com/feed",
                "--kind",
                "rss",
                "--category",
                "models",
            ],
        )
        code, out, _ = _run(engine, ["sources", "disable", "--url", "https://example.com/feed"])
        assert code == EXIT_OK
        assert "disabled source https://example.com/feed" in out

        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).one()
        assert bool(row.active) is False

    def test_disable_unknown_url_reports_not_found(self, engine: Engine) -> None:
        code, _, err = _run(engine, ["sources", "disable", "--url", "https://missing.example/feed"])
        assert code == EXIT_NOT_FOUND
        assert "no source with url https://missing.example/feed" in err


# ---------------------------------------------------------------------------
# issue build
# ---------------------------------------------------------------------------


class TestIssueBuild:
    def test_build_publishes_when_enough_items(self, engine: Engine) -> None:
        with engine.begin() as conn:
            _seed_publishable_clusters(conn)

        code, out, _ = _run(engine, ["issue", "build"])
        assert code == EXIT_OK
        assert "status=published" in out
        # week_of is the Monday of NOW (2026-07-27 is a Monday, so the same date).
        assert "week_of=2026-07-27" in out

        with engine.connect() as conn:
            row = conn.execute(select(issues_table)).one()
        assert row.status == "published"
        assert row.week_of == date(2026, 7, 27)

    def test_build_holds_when_too_few_items(self, engine: Engine) -> None:
        # Empty DB → no items → held.
        code, out, _ = _run(engine, ["issue", "build"])
        assert code == EXIT_OK
        assert "status=held" in out

        with engine.connect() as conn:
            row = conn.execute(select(issues_table)).one()
        assert row.status == "held"

    def test_build_honours_explicit_week(self, engine: Engine) -> None:
        code, out, _ = _run(engine, ["issue", "build", "--week", "2026-07-20"])
        assert code == EXIT_OK
        assert "week_of=2026-07-20" in out

        with engine.connect() as conn:
            row = conn.execute(select(issues_table)).one()
        assert row.week_of == date(2026, 7, 20)

    def test_build_rejects_malformed_week(self, engine: Engine) -> None:
        with pytest.raises(SystemExit):
            _run(engine, ["issue", "build", "--week", "not-a-date"])

    def test_build_reports_conflict_on_existing_week(self, engine: Engine) -> None:
        _run(engine, ["issue", "build"])
        code, _, err = _run(engine, ["issue", "build"])
        assert code == EXIT_CONFLICT
        assert "already exists" in err


# ---------------------------------------------------------------------------
# issue verify
# ---------------------------------------------------------------------------


class TestIssueVerify:
    def _insert_issue_with_items(self, engine: Engine, urls: list[str]) -> int:
        with engine.begin() as conn:
            # A dummy cluster per URL so the FK holds.
            result = conn.execute(
                issues_table.insert()
                .values(week_of=date(2026, 7, 27), status="published", published_at=NOW)
                .returning(issues_table.c.id)
            )
            issue_id = int(result.scalar_one())
            for position, url in enumerate(urls, start=1):
                cluster_id = int(
                    conn.execute(
                        clusters_table.insert()
                        .values(primary_url=url, canonical_headline=url, category="models")
                        .returning(clusters_table.c.id)
                    ).scalar_one()
                )
                conn.execute(
                    items_table.insert().values(
                        issue_id=issue_id,
                        cluster_id=cluster_id,
                        category="models",
                        position=position,
                        headline=url,
                        summary=url,
                        primary_url=url,
                        extra_source_urls=[],
                    )
                )
        return issue_id

    def test_verify_by_week_keeps_live_and_drops_dead(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._insert_issue_with_items(
            engine,
            ["https://live.example/1", "https://dead.example/2"],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "dead" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(200)

        monkeypatch.setattr("signalweek.cli.verify_issue", _wrap_verify(handler))

        code, out, _ = _run(engine, ["issue", "verify", "--week", "2026-07-27"])
        assert code == EXIT_OK
        assert "checked=2" in out
        assert "kept=1" in out
        assert "dropped=1" in out

        with engine.connect() as conn:
            surviving_urls = [
                row.primary_url for row in conn.execute(select(items_table.c.primary_url)).all()
            ]
        assert surviving_urls == ["https://live.example/1"]

    def test_verify_reports_not_found_when_no_matching_issue(self, engine: Engine) -> None:
        code, _, err = _run(engine, ["issue", "verify", "--week", "2020-01-06"])
        assert code == EXIT_NOT_FOUND
        assert "no issue for week_of=2020-01-06" in err

    def test_verify_requires_selector(self, engine: Engine) -> None:
        with pytest.raises(SystemExit):
            _run(engine, ["issue", "verify"])

    def test_verify_by_issue_id(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        issue_id = self._insert_issue_with_items(engine, ["https://live.example/1"])

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        monkeypatch.setattr("signalweek.cli.verify_issue", _wrap_verify(handler))

        code, out, _ = _run(engine, ["issue", "verify", "--issue-id", str(issue_id)])
        assert code == EXIT_OK
        assert "kept=1" in out
        assert "dropped=0" in out


def _wrap_verify(handler):
    """Return a ``verify_issue`` replacement that hands the real one a mock client."""
    from signalweek.digest.verify import verify_issue as real_verify_issue

    def wrapper(bind, *, issue_id, client=None, timeout=10.0, user_agent="signalweek-verify"):
        mock_client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            return real_verify_issue(
                bind,
                issue_id=issue_id,
                client=mock_client,
                timeout=timeout,
                user_agent=user_agent,
            )
        finally:
            mock_client.close()

    return wrapper


# ---------------------------------------------------------------------------
# issue publish / hold
# ---------------------------------------------------------------------------


def _insert_bare_issue(engine: Engine, *, status: str) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            issues_table.insert()
            .values(
                week_of=date(2026, 7, 27),
                status=status,
                published_at=NOW if status == "published" else None,
            )
            .returning(issues_table.c.id)
        )
        return int(result.scalar_one())


class TestIssuePublish:
    def test_publish_releases_a_held_issue(self, engine: Engine) -> None:
        issue_id = _insert_bare_issue(engine, status="held")
        code, out, _ = _run(engine, ["issue", "publish", "--week", "2026-07-27"])
        assert code == EXIT_OK
        assert f"published issue id={issue_id}" in out

        with engine.connect() as conn:
            row = conn.execute(select(issues_table)).one()
        assert row.status == "published"
        assert row.published_at is not None

    def test_publish_by_issue_id_is_idempotent(self, engine: Engine) -> None:
        issue_id = _insert_bare_issue(engine, status="published")
        code, out, _ = _run(engine, ["issue", "publish", "--issue-id", str(issue_id)])
        assert code == EXIT_OK
        assert "already published" in out

    def test_publish_reports_not_found(self, engine: Engine) -> None:
        code, _, err = _run(engine, ["issue", "publish", "--week", "2026-07-27"])
        assert code == EXIT_NOT_FOUND
        assert "no issue for week_of=2026-07-27" in err

    def test_publish_stamps_now_from_injected_clock(self, engine: Engine) -> None:
        _insert_bare_issue(engine, status="held")
        pinned = datetime(2026, 8, 3, 15, 30, tzinfo=UTC)
        code, out, _ = _run(engine, ["issue", "publish", "--week", "2026-07-27"], now=pinned)
        assert code == EXIT_OK
        assert "2026-08-03T15:30:00+00:00" in out

        with engine.connect() as conn:
            row = conn.execute(select(issues_table)).one()
        # SQLite drops tzinfo on read; compare naive.
        assert row.published_at.replace(tzinfo=UTC) == pinned


class TestIssueHold:
    def test_hold_retracts_a_published_issue(self, engine: Engine) -> None:
        _insert_bare_issue(engine, status="published")
        code, out, _ = _run(engine, ["issue", "hold", "--week", "2026-07-27"])
        assert code == EXIT_OK
        assert "held issue id=" in out
        assert "previous status=published" in out

        with engine.connect() as conn:
            row = conn.execute(select(issues_table)).one()
        assert row.status == "held"
        assert row.published_at is None

    def test_hold_is_idempotent(self, engine: Engine) -> None:
        _insert_bare_issue(engine, status="held")
        code, out, _ = _run(engine, ["issue", "hold", "--week", "2026-07-27"])
        assert code == EXIT_OK
        assert "already held" in out

    def test_hold_reports_not_found(self, engine: Engine) -> None:
        code, _, err = _run(engine, ["issue", "hold", "--issue-id", "9999"])
        assert code == EXIT_NOT_FOUND
        assert "no issue with id=9999" in err


# ---------------------------------------------------------------------------
# Cross-cutting: no subscriber/send/user commands are wired up.
# ---------------------------------------------------------------------------


class TestNoAggregatorCommands:
    """The pivot removed per-subscriber surfaces — the CLI must not resurrect them."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["subscriber", "add", "--email", "a@example.com"],
            ["subscribers", "list"],
            ["users", "list"],
            ["issue", "send"],
            ["send", "issue"],
            ["email", "issue"],
            ["signup"],
        ],
    )
    def test_removed_commands_reject_with_usage_error(
        self, engine: Engine, argv: list[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _run(engine, argv)
        # argparse exits with 2 on unknown subcommand.
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Recency: build honours the current week from the injected clock.
# ---------------------------------------------------------------------------


def test_build_default_week_is_monday_of_now(engine: Engine) -> None:
    # NOW is Monday 2026-07-27, so a build without --week should target it.
    a_wednesday = NOW + timedelta(days=2)  # 2026-07-29
    code, out, _ = _run(engine, ["issue", "build"], now=a_wednesday)
    assert code == EXIT_OK
    assert "week_of=2026-07-27" in out
