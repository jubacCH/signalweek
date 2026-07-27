"""End-to-end tests for the public issue archive.

Two routes are pinned here:

* ``GET /issues`` — reverse-chronological list of *published* issues.
* ``GET /issues/{week_of}`` — the permalink page for a single week, rendered
  through the fixed 5-section issue renderer. Draft, held, and unknown weeks
  return 404 — editorial state must not leak.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

from signalweek.db.session import create_db_engine
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    sources_metadata,
)
from signalweek.web import create_app


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_db_engine("sqlite:///:memory:", poolclass=StaticPool)
    sources_metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine=engine)) as c:
        yield c


def _insert_cluster(
    conn: Connection,
    *,
    primary_url: str,
    category: str,
    headline: str,
) -> int:
    result = conn.execute(
        clusters_table.insert()
        .values(primary_url=primary_url, category=category, canonical_headline=headline)
        .returning(clusters_table.c.id)
    )
    return int(result.scalar_one())


def _insert_issue(
    conn: Connection,
    *,
    week_of: date,
    status: str,
    published_at: datetime | None,
) -> int:
    result = conn.execute(
        issues_table.insert()
        .values(week_of=week_of, status=status, published_at=published_at)
        .returning(issues_table.c.id)
    )
    return int(result.scalar_one())


def _insert_item(
    conn: Connection,
    *,
    issue_id: int,
    cluster_id: int,
    category: str,
    position: int,
    headline: str,
    summary: str,
    primary_url: str,
    extra_source_urls: list[str] | None = None,
) -> None:
    conn.execute(
        items_table.insert().values(
            issue_id=issue_id,
            cluster_id=cluster_id,
            category=category,
            position=position,
            headline=headline,
            summary=summary,
            primary_url=primary_url,
            extra_source_urls=extra_source_urls or [],
        )
    )


def _seed_published_issue(
    engine: Engine,
    *,
    week_of: date,
    published_at: datetime,
    headline: str = "OpenAI releases GPT-6",
    primary_url: str = "https://openai.example/blog/gpt6",
    category: str = "models",
    summary: str = "A 2M-token context window and a new agentic mode.",
    extra_source_urls: list[str] | None = None,
) -> int:
    with engine.begin() as conn:
        issue_id = _insert_issue(
            conn, week_of=week_of, status="published", published_at=published_at
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url=primary_url,
            category=category,
            headline=headline,
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cluster_id,
            category=category,
            position=1,
            headline=headline,
            summary=summary,
            primary_url=primary_url,
            extra_source_urls=extra_source_urls,
        )
    return issue_id


# --------------------------------------------------------------------------- #
# /issues — the archive index
# --------------------------------------------------------------------------- #


def test_issues_index_renders_empty_state(client: TestClient) -> None:
    response = client.get("/issues")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Signalweek archive" in body
    assert "No issues have been published yet" in body


def test_issues_index_lists_published_issues_reverse_chronologically(
    client: TestClient, engine: Engine
) -> None:
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 6),
        published_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        headline="Older-Week-Story",
        primary_url="https://a.example/x",
    )
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        headline="Newer-Week-Story",
        primary_url="https://b.example/y",
    )
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 13),
        published_at=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        headline="Middle-Week-Story",
        primary_url="https://c.example/z",
    )

    body = client.get("/issues").text
    for week in ("2026-07-06", "2026-07-13", "2026-07-20"):
        assert week in body
    i_newer = body.index("2026-07-20")
    i_middle = body.index("2026-07-13")
    i_older = body.index("2026-07-06")
    assert i_newer < i_middle < i_older


def test_issues_index_links_to_permalinks(client: TestClient, engine: Engine) -> None:
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    body = client.get("/issues").text
    assert 'href="/issues/2026-07-20"' in body


def test_issues_index_excludes_draft_and_held_issues(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        _insert_issue(conn, week_of=date(2026, 6, 29), status="draft", published_at=None)
        _insert_issue(conn, week_of=date(2026, 7, 6), status="held", published_at=None)
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    body = client.get("/issues").text
    assert "2026-07-20" in body
    assert "2026-06-29" not in body
    assert "2026-07-06" not in body


def test_issues_index_shows_item_count_per_issue(client: TestClient, engine: Engine) -> None:
    week = date(2026, 7, 20)
    with engine.begin() as conn:
        issue_id = _insert_issue(
            conn,
            week_of=week,
            status="published",
            published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )
        for pos, cat in enumerate(("models", "funding", "research"), start=1):
            cid = _insert_cluster(
                conn,
                primary_url=f"https://x{pos}.example/y",
                category=cat,
                headline=f"H-{pos}",
            )
            _insert_item(
                conn,
                issue_id=issue_id,
                cluster_id=cid,
                category=cat,
                position=pos,
                headline=f"H-{pos}",
                summary=f"S-{pos}",
                primary_url=f"https://x{pos}.example/y",
            )
    body = client.get("/issues").text
    assert "3 stories" in body


# --------------------------------------------------------------------------- #
# /issues/{week_of} — the permalink page
# --------------------------------------------------------------------------- #


def test_issue_permalink_renders_full_five_section_page(client: TestClient, engine: Engine) -> None:
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        headline="Acme raises Series C",
        primary_url="https://acme.example/press",
        category="funding",
        summary="Acme raised $200M in a Series C led by Sequoia.",
    )
    response = client.get("/issues/2026-07-20")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "2026-07-20" in body
    assert "Acme raises Series C" in body
    assert 'href="https://acme.example/press"' in body
    assert "Acme raised $200M in a Series C led by Sequoia." in body
    # Fixed 5-section chrome from issue.html.j2 is used.
    assert 'id="digest-permalink"' in body
    for label in ("Models", "Lawsuits", "Funding", "Research", "Industry moves"):
        assert label in body


def test_issue_permalink_marks_page_as_published_with_timestamp(
    client: TestClient, engine: Engine
) -> None:
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    body = client.get("/issues/2026-07-20").text
    assert "Published" in body
    assert 'datetime="2026-07-20T09:00:00+00:00"' in body
    # Draft/held preview banner must not render for published issues.
    assert "Preview" not in body


def test_issue_permalink_renders_extra_source_urls(client: TestClient, engine: Engine) -> None:
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        extra_source_urls=[
            "https://mirror-one.example/a",
            "https://mirror-two.example/b",
        ],
    )
    body = client.get("/issues/2026-07-20").text
    assert "Also covered by" in body
    assert 'href="https://mirror-one.example/a"' in body
    assert 'href="https://mirror-two.example/b"' in body


def test_issue_permalink_404_for_unknown_week(client: TestClient) -> None:
    response = client.get("/issues/2026-07-20")
    assert response.status_code == 404


def test_issue_permalink_404_for_draft_issue(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        issue_id = _insert_issue(conn, week_of=date(2026, 7, 20), status="draft", published_at=None)
        cid = _insert_cluster(
            conn,
            primary_url="https://draft.example/x",
            category="models",
            headline="Draft-Only-Headline",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cid,
            category="models",
            position=1,
            headline="Draft-Only-Headline",
            summary="Ignored while draft.",
            primary_url="https://draft.example/x",
        )
    response = client.get("/issues/2026-07-20")
    assert response.status_code == 404
    assert "Draft-Only-Headline" not in response.text


def test_issue_permalink_404_for_held_issue(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        issue_id = _insert_issue(conn, week_of=date(2026, 7, 20), status="held", published_at=None)
        cid = _insert_cluster(
            conn,
            primary_url="https://held.example/x",
            category="models",
            headline="Held-Only-Headline",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cid,
            category="models",
            position=1,
            headline="Held-Only-Headline",
            summary="Ignored while held.",
            primary_url="https://held.example/x",
        )
    response = client.get("/issues/2026-07-20")
    assert response.status_code == 404
    assert "Held-Only-Headline" not in response.text


@pytest.mark.parametrize(
    "path",
    ["/issues/not-a-date", "/issues/2026-13-01", "/issues/2026-07"],
)
def test_issue_permalink_404_for_malformed_week(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404


def test_issue_permalink_escapes_untrusted_item_text(client: TestClient, engine: Engine) -> None:
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 20),
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        headline="<script>alert(1)</script>",
        summary="<img src=x onerror=alert(1)>",
        primary_url="https://x.example/y",
    )
    body = client.get("/issues/2026-07-20").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
