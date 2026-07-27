"""Tests for :mod:`signalweek.digest.verify`.

The verifier probes each item's ``primary_url`` at publish time and drops
the ``items`` rows whose link is not reachable. Network calls are stubbed
with :class:`httpx.MockTransport` so no real HTTP fires during the suite.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.engine import Connection, Engine

from signalweek.db.session import create_db_engine
from signalweek.digest.verify import verify_issue
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    sources_metadata,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures + seeders
# ---------------------------------------------------------------------------


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _insert_issue(conn: Connection) -> int:
    result = conn.execute(
        issues_table.insert()
        .values(week_of=date(2026, 7, 27), status="draft", published_at=None)
        .returning(issues_table.c.id)
    )
    return int(result.scalar_one())


def _insert_items(conn: Connection, *, issue_id: int, urls: list[str]) -> list[int]:
    ids: list[int] = []
    for position, url in enumerate(urls, start=1):
        cluster_result = conn.execute(
            clusters_table.insert()
            .values(primary_url=url, category="models", canonical_headline=f"Headline {position}")
            .returning(clusters_table.c.id)
        )
        cluster_id = int(cluster_result.scalar_one())
        item_result = conn.execute(
            items_table.insert()
            .values(
                issue_id=issue_id,
                cluster_id=cluster_id,
                category="models",
                position=position,
                headline=f"Headline {position}",
                summary="s",
                primary_url=url,
                extra_source_urls=[],
            )
            .returning(items_table.c.id)
        )
        ids.append(int(item_result.scalar_one()))
    return ids


def _remaining_urls(conn: Connection, issue_id: int) -> list[str]:
    rows = conn.execute(
        items_table.select().where(items_table.c.issue_id == issue_id).order_by(items_table.c.id)
    ).all()
    return [r.primary_url for r in rows]


# ---------------------------------------------------------------------------
# Empty issue
# ---------------------------------------------------------------------------


def test_verify_with_no_items_is_a_no_op(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)

    def _never_called(_: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("no HTTP calls should have been made")

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_never_called))

    assert result.checked == 0
    assert result.kept == 0
    assert result.dropped == 0
    assert result.drop_rate == 0.0


# ---------------------------------------------------------------------------
# Happy path: every link is live
# ---------------------------------------------------------------------------


def test_all_live_links_are_kept(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(
            conn,
            issue_id=issue_id,
            urls=[
                "https://openai.com/blog/a",
                "https://openai.com/blog/b",
                "https://openai.com/blog/c",
            ],
        )

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        return httpx.Response(200)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.checked == 3
    assert result.kept == 3
    assert result.dropped == 0
    assert result.drop_rate == 0.0
    # Every probe is a HEAD — no GET fallback needed on a 200.
    assert all(entry.startswith("HEAD ") for entry in calls)

    with curated_engine.begin() as conn:
        assert len(_remaining_urls(conn, issue_id)) == 3


# ---------------------------------------------------------------------------
# Non-200 responses drop the row
# ---------------------------------------------------------------------------


def test_items_returning_404_are_dropped(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(
            conn,
            issue_id=issue_id,
            urls=[
                "https://ok.example/1",
                "https://gone.example/2",
                "https://ok.example/3",
            ],
        )

    def _handler(request: httpx.Request) -> httpx.Response:
        if "gone.example" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.checked == 3
    assert result.kept == 2
    assert result.dropped == 1
    assert result.drop_rate == pytest.approx(1 / 3)
    assert [d.primary_url for d in result.dropped_items] == ["https://gone.example/2"]
    assert result.dropped_items[0].status_code == 404
    assert result.dropped_items[0].reason == "http_404"

    with curated_engine.begin() as conn:
        remaining = _remaining_urls(conn, issue_id)
    assert remaining == ["https://ok.example/1", "https://ok.example/3"]


def test_5xx_responses_are_dropped(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(conn, issue_id=issue_id, urls=["https://broken.example/x"])

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.dropped == 1
    assert result.kept == 0
    assert result.dropped_items[0].status_code == 503


# ---------------------------------------------------------------------------
# HEAD → GET fallback
# ---------------------------------------------------------------------------


def test_head_405_falls_back_to_get_and_keeps_when_get_is_200(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(conn, issue_id=issue_id, urls=["https://picky.example/x"])

    seen_methods: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert seen_methods == ["HEAD", "GET"]
    assert result.kept == 1
    assert result.dropped == 0


def test_head_501_falls_back_to_get_and_drops_when_get_is_404(
    curated_engine: Engine,
) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(conn, issue_id=issue_id, urls=["https://picky.example/x"])

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(501)
        return httpx.Response(404)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.dropped == 1
    assert result.dropped_items[0].status_code == 404


# ---------------------------------------------------------------------------
# Redirects and network errors
# ---------------------------------------------------------------------------


def test_redirect_to_200_is_kept(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(conn, issue_id=issue_id, urls=["https://old.example/x"])

    def _handler(request: httpx.Request) -> httpx.Response:
        if "old.example" in str(request.url):
            return httpx.Response(301, headers={"Location": "https://new.example/x"})
        return httpx.Response(200)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.kept == 1
    assert result.dropped == 0


def test_network_error_counts_as_a_drop(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(conn, issue_id=issue_id, urls=["https://dead.example/x"])

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.dropped == 1
    dropped = result.dropped_items[0]
    assert dropped.status_code is None
    assert dropped.reason == "ConnectError"

    with curated_engine.begin() as conn:
        assert _remaining_urls(conn, issue_id) == []


# ---------------------------------------------------------------------------
# Drop-rate logging
# ---------------------------------------------------------------------------


def test_drop_rate_is_logged(curated_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    with curated_engine.begin() as conn:
        issue_id = _insert_issue(conn)
        _insert_items(
            conn,
            issue_id=issue_id,
            urls=["https://ok.example/a", "https://gone.example/b"],
        )

    def _handler(request: httpx.Request) -> httpx.Response:
        if "gone.example" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200)

    with caplog.at_level(logging.INFO, logger="signalweek.digest.verify"):
        with curated_engine.begin() as conn:
            result = verify_issue(conn, issue_id=issue_id, client=_mock_client(_handler))

    assert result.drop_rate == pytest.approx(0.5)
    assert any(
        f"issue_id={issue_id}" in record.message
        and "checked=2" in record.message
        and "dropped=1" in record.message
        and "drop_rate=0.50" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Issue isolation: only the target issue is touched
# ---------------------------------------------------------------------------


def test_only_target_issue_items_are_probed_and_deleted(curated_engine: Engine) -> None:
    with curated_engine.begin() as conn:
        target_id = _insert_issue(conn)
        _insert_items(conn, issue_id=target_id, urls=["https://ok.example/target"])
        other_result = conn.execute(
            issues_table.insert()
            .values(week_of=date(2026, 7, 20), status="published", published_at=NOW)
            .returning(issues_table.c.id)
        )
        other_id = int(other_result.scalar_one())
        _insert_items(conn, issue_id=other_id, urls=["https://gone.example/other"])

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    with curated_engine.begin() as conn:
        result = verify_issue(conn, issue_id=target_id, client=_mock_client(_handler))

    assert result.checked == 1
    assert calls == ["https://ok.example/target"]

    with curated_engine.begin() as conn:
        assert _remaining_urls(conn, other_id) == ["https://gone.example/other"]
