"""Acceptance tests for the in-scope spec criteria.

These tests pin the seven guarantees the curated-digest MVP owes its spec:

1. The landing page returns ``200`` with the product name and a link to the
   latest published issue.
2. The archive index and permalink routes both work.
3. Every item that ends up in a *published* issue has a category from the
   fixed five and a ``primary_url`` that returned ``200`` at publish time
   (the ``verify`` pass drops anything else before the issue is served).
4. The classifier never emits ``Uncategorized`` — every input lands in one
   of the five approved buckets.
5. The builder holds — never publishes — an issue with fewer than 10 items.
6. The 12-week cross-issue URL dedup guard rejects a repeat URL.
7. No per-user routes (sign-up, log in, log out, sources CRUD, personal
   digest, subscribe) remain on the FastAPI app.

The tests are deliberately end-to-end where they need to be (routes, build +
verify) and unit-level where the spec point is a pure-function guarantee
(the classifier's total function contract).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

from signalweek.db.session import create_db_engine
from signalweek.digest.builder import DEFAULT_MIN_ITEMS, build_issue
from signalweek.digest.verify import verify_issue
from signalweek.ingest.classify import (
    CATEGORIES,
    FALLBACK_CATEGORY,
    classify_clusters,
    classify_text,
)
from signalweek.sources import (
    clusters_table,
    issues_table,
    items_table,
    raw_items_table,
    sources_metadata,
    sources_table,
)
from signalweek.web import create_app

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)  # a Monday
MONDAY = NOW.date()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite with the curated-digest schema, shared across threads.

    ``StaticPool`` keeps a single connection alive so the schema created in
    the fixture is visible to the FastAPI TestClient worker.
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


# --------------------------------------------------------------------------- #
# Small seed helpers — kept local so each test reads top-to-bottom.
# --------------------------------------------------------------------------- #


def _insert_source(conn: Connection, *, url: str, category_hint: str = "models") -> int:
    result = conn.execute(
        sources_table.insert()
        .values(url=url, kind="rss", category_hint=category_hint, active=True)
        .returning(sources_table.c.id)
    )
    return int(result.scalar_one())


def _insert_raw_item(
    conn: Connection,
    *,
    source_id: int,
    url: str,
    title: str,
    canonical_url: str | None = None,
    body: str | None = None,
    first_seen_at: datetime = NOW,
) -> int:
    result = conn.execute(
        raw_items_table.insert()
        .values(
            source_id=source_id,
            url=url,
            canonical_url=canonical_url or url,
            title=title,
            body=body,
            fetched_at=first_seen_at,
            first_seen_at=first_seen_at,
        )
        .returning(raw_items_table.c.id)
    )
    return int(result.scalar_one())


def _insert_cluster(
    conn: Connection,
    *,
    primary_url: str,
    canonical_headline: str,
    category: str = "models",
) -> int:
    result = conn.execute(
        clusters_table.insert()
        .values(
            primary_url=primary_url,
            canonical_headline=canonical_headline,
            category=category,
        )
        .returning(clusters_table.c.id)
    )
    return int(result.scalar_one())


def _insert_issue(
    conn: Connection,
    *,
    week_of: date,
    status: str = "published",
    published_at: datetime | None = NOW,
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
    primary_url: str,
) -> None:
    conn.execute(
        items_table.insert().values(
            issue_id=issue_id,
            cluster_id=cluster_id,
            category=category,
            position=position,
            headline=headline,
            summary=headline,
            primary_url=primary_url,
            extra_source_urls=[],
        )
    )


def _seed_full_week_of_candidates(conn: Connection) -> None:
    """Seed 11 recent clusters spanning the five categories.

    This comfortably clears :data:`DEFAULT_MIN_ITEMS` so a build off this
    fixture ends up published rather than held.
    """
    s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
    seeds = [
        ("models", "OpenAI unveils GPT-5", "https://openai.com/blog/gpt-5"),
        ("models", "Anthropic releases Claude 5", "https://openai.com/blog/claude-5"),
        ("models", "Google launches Gemini 3", "https://openai.com/blog/gemini-3"),
        (
            "funding",
            "Anthropic raises Series F at $60B valuation",
            "https://openai.com/blog/anthropic-round",
        ),
        (
            "funding",
            "Startup raises $200M Series C",
            "https://openai.com/blog/startup-round",
        ),
        (
            "lawsuits_policy",
            "Court rules against major AI vendor",
            "https://openai.com/blog/court-ruling",
        ),
        (
            "lawsuits_policy",
            "Executive order signed on AI safety",
            "https://openai.com/blog/eo-ai",
        ),
        (
            "research",
            "New preprint proposes novel benchmark",
            "https://openai.com/blog/preprint",
        ),
        (
            "research",
            "Researchers report SOTA on reasoning benchmark",
            "https://openai.com/blog/sota",
        ),
        (
            "industry_moves",
            "Google hires new CEO for AI division",
            "https://openai.com/blog/hire-ceo",
        ),
        (
            "industry_moves",
            "Meta announces layoffs across AI org",
            "https://openai.com/blog/layoffs",
        ),
    ]
    for category, headline, url in seeds:
        _insert_raw_item(
            conn,
            source_id=s,
            url=url,
            title=headline,
            body=f"{headline}. Extended context follows this lede for the item summary.",
        )
        _insert_cluster(
            conn,
            primary_url=url,
            canonical_headline=headline,
            category=category,
        )


# --------------------------------------------------------------------------- #
# 1. Landing returns 200 with product name + latest-issue link
# --------------------------------------------------------------------------- #


def test_landing_returns_200_with_product_name_and_latest_issue_link(
    client: TestClient, engine: Engine
) -> None:
    with engine.begin() as conn:
        issue_id = _insert_issue(conn, week_of=date(2026, 7, 20), published_at=NOW)
        cid = _insert_cluster(
            conn,
            primary_url="https://openai.example/blog/gpt6",
            canonical_headline="OpenAI releases GPT-6",
            category="models",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cid,
            category="models",
            position=1,
            headline="OpenAI releases GPT-6",
            primary_url="https://openai.example/blog/gpt6",
        )

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # Product name appears as the hero H1, not just in the site chrome.
    assert "<h1>Signalweek</h1>" in body
    # The landing surfaces a link to the most recent issue's on-page preview
    # (an in-page anchor rendered only when a published issue exists).
    assert 'href="#latest-issue"' in body
    assert 'id="latest-issue"' in body
    # The preview identifies the issue by its week_of date.
    assert "2026-07-20" in body
    # The seeded item's primary source is linked from the preview.
    assert 'href="https://openai.example/blog/gpt6"' in body


# --------------------------------------------------------------------------- #
# 2. Archive index + permalink routes
# --------------------------------------------------------------------------- #


def test_archive_index_route_returns_200_and_lists_published_issues(
    client: TestClient, engine: Engine
) -> None:
    with engine.begin() as conn:
        _insert_issue(conn, week_of=date(2026, 7, 6), published_at=NOW - timedelta(weeks=2))
        _insert_issue(conn, week_of=date(2026, 7, 20), published_at=NOW)

    response = client.get("/issues")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    for week in ("2026-07-06", "2026-07-20"):
        assert week in body
    # The index links to the permalink route so readers can drill in.
    assert 'href="/issues/2026-07-20"' in body
    assert 'href="/issues/2026-07-06"' in body


def test_permalink_route_returns_200_for_a_published_week(
    client: TestClient, engine: Engine
) -> None:
    week = date(2026, 7, 20)
    with engine.begin() as conn:
        issue_id = _insert_issue(conn, week_of=week, published_at=NOW)
        cid = _insert_cluster(
            conn,
            primary_url="https://acme.example/press",
            canonical_headline="Acme raises Series C",
            category="funding",
        )
        _insert_item(
            conn,
            issue_id=issue_id,
            cluster_id=cid,
            category="funding",
            position=1,
            headline="Acme raises Series C",
            primary_url="https://acme.example/press",
        )

    response = client.get("/issues/2026-07-20")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Acme raises Series C" in body
    assert 'href="https://acme.example/press"' in body


def test_permalink_route_returns_404_for_unknown_week(client: TestClient) -> None:
    assert client.get("/issues/2026-07-20").status_code == 404


# --------------------------------------------------------------------------- #
# 3. Every published item has a category in the fixed 5 and a primary_url
#    that returned 200 at publish (build → verify guarantees).
# --------------------------------------------------------------------------- #


def test_every_published_item_has_a_fixed_category_and_a_live_primary_url(
    engine: Engine,
) -> None:
    with engine.begin() as conn:
        _seed_full_week_of_candidates(conn)
        # A dead link mixed in — the verifier must delete this row before the
        # issue is considered "published to the public".
        dead_source = _insert_source(conn, url="https://dead.example/rss.xml")
        _insert_raw_item(
            conn,
            source_id=dead_source,
            url="https://dead.example/story",
            title="Startup raises seed round",
            body="Body.",
        )
        _insert_cluster(
            conn,
            primary_url="https://dead.example/story",
            canonical_headline="Startup raises seed round",
            category="funding",
        )

    with engine.begin() as conn:
        build_result = build_issue(conn, now=NOW)

    assert build_result.status == "published"

    probed: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        probed.append(url)
        if "dead.example" in url:
            return httpx.Response(404)
        return httpx.Response(200)

    with engine.begin() as conn:
        verify_result = verify_issue(
            conn,
            issue_id=build_result.issue_id,
            client=httpx.Client(transport=httpx.MockTransport(_handler)),
        )

    # The dead link was dropped; every surviving item was probed and got 200.
    assert verify_result.dropped >= 1
    assert verify_result.kept >= DEFAULT_MIN_ITEMS
    for dropped in verify_result.dropped_items:
        assert dropped.status_code != 200

    with engine.begin() as conn:
        surviving = conn.execute(
            items_table.select().where(items_table.c.issue_id == build_result.issue_id)
        ).all()

    assert len(surviving) == verify_result.kept
    surviving_urls = {row.primary_url for row in surviving}
    # None of the dropped URLs are still attached to the published issue.
    for dropped in verify_result.dropped_items:
        assert dropped.primary_url not in surviving_urls
    # Every surviving item is in one of the five fixed categories.
    for row in surviving:
        assert row.category in CATEGORIES
    # And every surviving primary_url is one the verifier saw a 200 for.
    for row in surviving:
        assert row.primary_url in probed


# --------------------------------------------------------------------------- #
# 4. Classifier never emits Uncategorized.
# --------------------------------------------------------------------------- #


def test_classify_text_never_returns_uncategorized(engine: Engine) -> None:
    """The pure function is total: every input maps into the fixed five."""
    inputs = [
        "",
        "   ",
        "totally unrelated marketing chatter",
        "​​",  # zero-width whitespace only
        "OpenAI unveils GPT-5 with a bigger context window",
        "European Commission fines vendor",
        "Anthropic raises Series F at $60B valuation",
        "Researchers report SOTA on reasoning benchmark",
        "Google hires new CEO for AI division",
        "some random text with no signal at all",
    ]
    for text in inputs:
        chosen = classify_text(text)
        assert chosen in CATEGORIES, f"{text!r} → {chosen!r}"
        assert chosen.lower() != "uncategorized"
    # The catch-all fallback itself is one of the five, not an escape hatch.
    assert FALLBACK_CATEGORY in CATEGORIES


def test_classify_clusters_never_writes_uncategorized(engine: Engine) -> None:
    """End-to-end: after a run, every cluster.category is one of the five."""
    with engine.begin() as conn:
        # A source without a category_hint so the hint tiebreak is not
        # available — the classifier must still land inside CATEGORIES.
        s = _insert_source(conn, url="https://noise.example/rss.xml", category_hint="models")
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://noise.example/a",
            title="totally unrelated",
        )
        _insert_cluster(
            conn,
            primary_url="https://noise.example/a",
            canonical_headline="totally unrelated marketing chatter",
            # Seed the row with an out-of-taxonomy value; the run must
            # overwrite it with one of the fixed five.
            category="uncategorized",
        )
        _insert_raw_item(
            conn,
            source_id=s,
            url="https://noise.example/b",
            title="OpenAI unveils GPT-5",
        )
        _insert_cluster(
            conn,
            primary_url="https://noise.example/b",
            canonical_headline="OpenAI unveils GPT-5",
            category="uncategorized",
        )

    with engine.begin() as conn:
        result = classify_clusters(conn)

    assert result.total == 2
    for cluster_id, chosen in result.categories.items():
        assert chosen in CATEGORIES, f"cluster {cluster_id} → {chosen!r}"
    with engine.begin() as conn:
        rows = conn.execute(clusters_table.select()).all()
    assert rows
    for row in rows:
        assert row.category in CATEGORIES


# --------------------------------------------------------------------------- #
# 5. Builder holds an issue with < 10 items.
# --------------------------------------------------------------------------- #


def test_builder_holds_an_issue_with_fewer_than_ten_items(engine: Engine) -> None:
    with engine.begin() as conn:
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        # Seed only 5 candidates — well below the DEFAULT_MIN_ITEMS threshold.
        for idx in range(5):
            url = f"https://openai.com/blog/story-{idx}"
            _insert_raw_item(
                conn,
                source_id=s,
                url=url,
                title=f"OpenAI unveils model {idx}",
                first_seen_at=NOW - timedelta(hours=idx),
            )
            _insert_cluster(
                conn,
                primary_url=url,
                canonical_headline=f"OpenAI unveils model {idx}",
                category="models",
            )

    with engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert DEFAULT_MIN_ITEMS == 10
    assert result.total_items < DEFAULT_MIN_ITEMS
    assert result.status == "held"

    with engine.begin() as conn:
        row = conn.execute(issues_table.select().where(issues_table.c.id == result.issue_id)).one()
    assert row.status == "held"
    # Held issues never receive a published_at timestamp.
    assert row.published_at is None


# --------------------------------------------------------------------------- #
# 6. 12-week cross-issue URL dedup rejects a repeat URL.
# --------------------------------------------------------------------------- #


def test_twelve_week_cross_issue_dedup_rejects_a_repeat_url(engine: Engine) -> None:
    repeat_url = "https://openai.com/blog/gpt-5"
    with engine.begin() as conn:
        # A previously-published issue that already ran the story.
        prior_id = _insert_issue(
            conn,
            week_of=MONDAY - timedelta(weeks=1),
            published_at=NOW - timedelta(weeks=1),
        )
        prior_cid = _insert_cluster(
            conn,
            primary_url=repeat_url,
            canonical_headline="Old story",
        )
        _insert_item(
            conn,
            issue_id=prior_id,
            cluster_id=prior_cid,
            category="models",
            position=1,
            headline="Old story",
            primary_url=repeat_url,
        )

        # A fresh candidate this week that points at the same URL.
        s = _insert_source(conn, url="https://openai.com/blog/rss.xml")
        _insert_raw_item(
            conn,
            source_id=s,
            url=repeat_url,
            title="OpenAI unveils GPT-5",
        )
        _insert_cluster(
            conn,
            primary_url=repeat_url,
            canonical_headline="OpenAI unveils GPT-5",
        )

    with engine.begin() as conn:
        result = build_issue(conn, now=NOW)

    assert result.rejected_by_dedup == 1
    # No items landed — the candidate was the only one this week.
    assert result.total_items == 0
    with engine.begin() as conn:
        items = conn.execute(
            items_table.select().where(items_table.c.issue_id == result.issue_id)
        ).all()
    assert items == []


# --------------------------------------------------------------------------- #
# 7. No user/session/login routes remain.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/signup",
        "/login",
        "/logout",
        "/register",
        "/session",
        "/sessions",
        "/account",
        "/profile",
        "/users",
        "/user",
        "/me",
        "/subscribe",
        "/unsubscribe",
        "/sources",
        "/api/sources",
        "/api/signals",
        "/digest",
        "/digest/history",
        "/digest/2026-W30",
        "/digest/2026-W30.md",
    ],
)
def test_user_session_and_login_routes_are_absent(client: TestClient, path: str) -> None:
    """None of the retired per-user surfaces respond to any HTTP method.

    The routes must be removed outright — a 401/403 would mean the surface
    still exists behind an auth wall, which the pivot forbids.
    """
    for method in ("GET", "POST"):
        response = client.request(method, path)
        assert response.status_code == 404, (
            f"{method} {path} returned {response.status_code}, expected 404 "
            "(per-user routes must be removed, not gated)"
        )


def test_registered_routes_are_limited_to_the_public_surface(client: TestClient) -> None:
    """Enumerate the mounted paths and check nothing user-scoped snuck in."""
    app = client.app
    # ``routes`` includes both endpoint routes and mounts (e.g. /static).
    paths = {getattr(r, "path", "") for r in app.routes}
    # The public surface pinned by the spec.
    expected = {"/", "/issues", "/issues/{week_of}", "/health", "/static"}
    assert expected.issubset(paths), f"missing routes: {expected - paths}"
    # Forbid any path that mentions a user/session/subscription concept.
    forbidden_terms = (
        "signup",
        "login",
        "logout",
        "register",
        "session",
        "account",
        "profile",
        "user",
        "subscribe",
        "unsubscribe",
        "digest",
    )
    for path in paths:
        lowered = path.lower()
        for term in forbidden_terms:
            assert term not in lowered, f"route {path!r} mentions forbidden term {term!r}"
