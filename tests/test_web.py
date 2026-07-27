"""End-to-end tests for the FastAPI web layer.

The web layer is deliberately minimal for the curated-digest pivot: a health
check and a public landing page. Every per-user surface (sign-up, log in,
sources CRUD, personal digest) has been retired.
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
    """A fresh in-memory SQLite engine with the curated-digest schema.

    ``StaticPool`` keeps one shared connection across threads so the schema
    created here is visible to the FastAPI TestClient's worker thread.
    """
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
    week_of: date = date(2026, 7, 20),
    published_at: datetime = datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
) -> int:
    """Seed a small published issue and return its id."""
    with engine.begin() as conn:
        issue_id = _insert_issue(
            conn, week_of=week_of, status="published", published_at=published_at
        )
        cluster_id = _insert_cluster(
            conn,
            primary_url="https://openai.example/blog/gpt6",
            category="models",
            headline="OpenAI releases GPT-6",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cluster_id,
            category="models",
            position=1,
            headline="OpenAI releases GPT-6",
            summary="A 2M-token context window and a new agentic mode.",
            primary_url="https://openai.example/blog/gpt6",
        )
        cluster_id_2 = _insert_cluster(
            conn,
            primary_url="https://acme.example/press",
            category="funding",
            headline="Acme raises Series C",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cluster_id_2,
            category="funding",
            position=2,
            headline="Acme raises Series C",
            summary="Acme raised $200M in a Series C led by Sequoia.",
            primary_url="https://acme.example/press",
        )
    return issue_id


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_landing_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Signalweek" in body
    # Pico.css is linked from the base template.
    assert "pico.min.css" in body


def test_landing_shows_product_name_and_one_sentence_description(
    client: TestClient,
) -> None:
    body = client.get("/").text
    # Product name appears as the hero H1, not just in the site chrome.
    assert "<h1>Signalweek</h1>" in body
    # A one-sentence description of the product is rendered.
    assert "curated weekly digest of the AI industry" in body


def test_landing_page_uses_curated_digest_framing(client: TestClient) -> None:
    body = client.get("/").text
    # Fixed five-category taxonomy is surfaced on the landing page.
    for category in ("Models", "Lawsuits", "Funding", "Research", "Industry moves"):
        assert category in body


def test_landing_page_has_no_user_account_ctas(client: TestClient) -> None:
    body = client.get("/").text
    # The product has no accounts — sign-up / login must not appear anywhere.
    for path in ("/signup", "/login", "/logout"):
        assert path not in body


def test_landing_page_has_no_email_signup_form(client: TestClient) -> None:
    body = client.get("/").text
    # Email delivery is deferred — no subscribe/signup form yet.
    assert "<form" not in body
    assert 'type="email"' not in body


def test_landing_without_published_issues_shows_coming_soon(
    client: TestClient,
) -> None:
    body = client.get("/").text
    assert "first issue is on its way" in body.lower()
    # No published issue means no "This week's issue" preview block.
    assert "This week&rsquo;s issue" not in body


def test_landing_with_published_issue_shows_preview_link(
    client: TestClient, engine: Engine
) -> None:
    _seed_published_issue(engine)
    body = client.get("/").text
    # The link CTA to the most recent published issue is visible.
    assert "Read this week" in body
    # The preview section itself renders.
    assert 'id="latest-issue"' in body
    # Week-of appears on the preview.
    assert "2026-07-20" in body
    # Both seeded headlines are linked to their primary source.
    assert 'href="https://openai.example/blog/gpt6"' in body
    assert "OpenAI releases GPT-6" in body
    assert 'href="https://acme.example/press"' in body
    assert "Acme raises Series C" in body


def test_landing_returns_only_the_most_recently_published_issue(
    client: TestClient, engine: Engine
) -> None:
    # Older published issue.
    _seed_published_issue(
        engine,
        week_of=date(2026, 7, 6),
        published_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
    )
    # Newer published issue with a distinctive headline.
    with engine.begin() as conn:
        newer_id = _insert_issue(
            conn,
            week_of=date(2026, 7, 20),
            status="published",
            published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )
        cid = _insert_cluster(
            conn,
            primary_url="https://newer.example/story",
            category="research",
            headline="Distinctive-Newer-Headline",
        )
        _insert_item(
            conn,
            issue_id=newer_id,
            cluster_id=cid,
            category="research",
            position=1,
            headline="Distinctive-Newer-Headline",
            summary="Newer summary.",
            primary_url="https://newer.example/story",
        )
    body = client.get("/").text
    assert "Distinctive-Newer-Headline" in body
    # The older issue's headline must not leak into the preview.
    assert "OpenAI releases GPT-6" not in body
    # Only the newer week is shown.
    assert "2026-07-20" in body
    assert "2026-07-06" not in body


def test_landing_ignores_held_and_draft_issues(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        held_id = _insert_issue(conn, week_of=date(2026, 7, 20), status="held", published_at=None)
        cid = _insert_cluster(
            conn,
            primary_url="https://held.example/story",
            category="models",
            headline="Held-Only-Headline",
        )
        _insert_item(
            conn,
            issue_id=held_id,
            cluster_id=cid,
            category="models",
            position=1,
            headline="Held-Only-Headline",
            summary="A held story.",
            primary_url="https://held.example/story",
        )
    body = client.get("/").text
    # Held issues must never leak onto the public landing page.
    assert "Held-Only-Headline" not in body
    assert "first issue is on its way" in body.lower()


def test_landing_escapes_untrusted_item_text(client: TestClient, engine: Engine) -> None:
    with engine.begin() as conn:
        issue_id = _insert_issue(
            conn,
            week_of=date(2026, 7, 20),
            status="published",
            published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )
        cid = _insert_cluster(
            conn,
            primary_url="https://x.example/y",
            category="models",
            headline="<script>alert(1)</script>",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cid,
            category="models",
            position=1,
            headline="<script>alert(1)</script>",
            summary="ignored on landing",
            primary_url="https://x.example/y",
        )
    body = client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_pico_css_is_served(client: TestClient) -> None:
    response = client.get("/static/pico.min.css")
    assert response.status_code == 200
    assert "Pico CSS" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/signup",
        "/login",
        "/logout",
        "/sources",
        "/digest",
        "/digest/history",
        "/digest/2026-W30",
        "/digest/2026-W30.md",
        "/api/sources",
        "/api/signals",
        "/api/digest/2026-W30",
    ],
)
def test_retired_user_surfaces_are_gone(client: TestClient, path: str) -> None:
    # The former per-user routes must return 404 — they've been removed, not
    # merely gated behind auth.
    response = client.request("GET", path)
    assert response.status_code == 404
