"""Tests for the digest builder and renderers.

The pure assembly path is unit-tested with hand-built ORM objects (constructed
without a session) so the ranking + sectioning behaviour is exercised without
touching the database. HTML and Markdown output are locked in via string
snapshots stored under ``tests/fixtures/digest/`` — set ``SNAPSHOT_UPDATE=1``
to regenerate them after an intentional template change.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from signalweek.db.models import Signal, Source
from signalweek.db.repositories import (
    SignalRepository,
    SourceRepository,
    UserRepository,
)
from signalweek.digest import (
    Digest,
    assemble_digest,
    build_digest,
    render_html,
    render_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "digest"

NOW = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(*, id: int, title: str | None, url: str, type: str = "rss") -> Source:
    src = Source(user_id=1, url=url, title=title, type=type)
    src.id = id
    return src


def _signal(
    *,
    id: int,
    source_id: int,
    title: str,
    url: str,
    summary: str | None,
    published_at: datetime | None,
) -> Signal:
    sig = Signal(
        source_id=source_id,
        guid=url,
        title=title,
        url=url,
        summary=summary,
        published_at=published_at,
    )
    sig.id = id
    return sig


def _canned_digest() -> Digest:
    src_blog = _source(id=1, title="Example Blog", url="https://blog.example.com/feed")
    src_hn = _source(
        id=2,
        title="HN Rust",
        url="https://hn.algolia.com/api/v1/search_by_date?query=rust",
        type="hackernews",
    )
    src_empty = _source(id=3, title="Empty Source", url="https://empty.example/feed")

    blog_new = _signal(
        id=11,
        source_id=1,
        title="Rolling out the new pipeline",
        url="https://blog.example.com/posts/new-pipeline",
        summary="How we shipped the new ingest pipeline in a week.",
        published_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
    )
    blog_metrics = _signal(
        id=12,
        source_id=1,
        title="Metrics that matter",
        url="https://blog.example.com/posts/metrics",
        summary=None,
        published_at=datetime(2026, 7, 17, 12, 30, tzinfo=UTC),
    )
    blog_old = _signal(
        id=13,
        source_id=1,
        title="Old post from before the window",
        url="https://blog.example.com/posts/old",
        summary="Should be filtered out.",
        published_at=datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
    )

    hn_rust = _signal(
        id=21,
        source_id=2,
        title="Rust 1.90 released",
        url="https://blog.rust-lang.org/2026/07/19/Rust-1.90.html",
        summary="Type inference improvements and a new borrow checker mode.",
        published_at=datetime(2026, 7, 19, 15, 0, tzinfo=UTC),
    )
    hn_show = _signal(
        id=22,
        source_id=2,
        title="Show HN: my new tool",
        url="https://example.com/tool",
        summary=None,
        published_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
    )

    # Deliberately shuffled input order so ordering asserts are meaningful.
    return assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[
            (src_blog, [blog_metrics, blog_new, blog_old]),
            (src_empty, []),
            (src_hn, [hn_show, hn_rust]),
        ],
        now=NOW,
    )


def _snapshot(rendered: str, name: str) -> None:
    """Compare ``rendered`` against ``FIXTURES/name`` (regenerable via env var)."""
    path = FIXTURES / name
    if os.environ.get("SNAPSHOT_UPDATE"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    expected = path.read_text()
    assert rendered == expected


# ---------------------------------------------------------------------------
# assemble_digest
# ---------------------------------------------------------------------------


def test_assemble_digest_orders_sections_by_top_item_score() -> None:
    digest = _canned_digest()

    # HN Rust wins the section race because its top item is fresher.
    assert [s.source_title for s in digest.sections] == ["HN Rust", "Example Blog"]


def test_assemble_digest_ranks_items_within_a_section() -> None:
    digest = _canned_digest()

    hn_section = digest.sections[0]
    blog_section = digest.sections[1]
    assert [i.title for i in hn_section.items] == [
        "Rust 1.90 released",
        "Show HN: my new tool",
    ]
    assert [i.title for i in blog_section.items] == [
        "Rolling out the new pipeline",
        "Metrics that matter",
    ]
    # Scores are strictly decreasing within each section.
    assert hn_section.items[0].score > hn_section.items[1].score
    assert blog_section.items[0].score > blog_section.items[1].score


def test_assemble_digest_drops_out_of_window_signals_and_empty_sections() -> None:
    digest = _canned_digest()

    all_titles = {item.title for section in digest.sections for item in section.items}
    assert "Old post from before the window" not in all_titles
    # The empty source has no in-window signals, so it must not appear as a section.
    assert "Empty Source" not in {s.source_title for s in digest.sections}


def test_assemble_digest_respects_max_items_per_section() -> None:
    src = _source(id=1, title="Chatty", url="https://chatty.example/feed")
    signals = [
        _signal(
            id=100 + i,
            source_id=1,
            title=f"Item {i}",
            url=f"https://chatty.example/{i}",
            summary=None,
            published_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        )
        for i in range(10)
    ]
    digest = assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[(src, signals)],
        now=NOW,
        max_items_per_section=3,
    )
    assert len(digest.sections) == 1
    assert len(digest.sections[0].items) == 3


def test_assemble_digest_falls_back_to_source_url_when_title_missing() -> None:
    src = _source(id=1, title=None, url="https://untitled.example/feed")
    sig = _signal(
        id=1,
        source_id=1,
        title="Hello",
        url="https://untitled.example/hello",
        summary=None,
        published_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )
    digest = assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[(src, [sig])],
        now=NOW,
    )
    assert digest.sections[0].source_title == "https://untitled.example/feed"


def test_assemble_digest_uses_keywords_to_reweight_ranking() -> None:
    src = _source(id=1, title="Feed", url="https://feed.example/")
    generic = _signal(
        id=1,
        source_id=1,
        title="Weekly newsletter",
        url="https://feed.example/newsletter",
        summary=None,
        published_at=datetime(2026, 7, 19, 20, 0, tzinfo=UTC),
    )
    niche = _signal(
        id=2,
        source_id=1,
        title="Deep dive on Rust",
        url="https://feed.example/rust",
        summary=None,
        published_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
    )

    baseline = assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[(src, [generic, niche])],
        now=NOW,
    )
    assert [i.title for i in baseline.sections[0].items] == [
        "Weekly newsletter",
        "Deep dive on Rust",
    ]

    boosted = assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[(src, [generic, niche])],
        now=NOW,
        keywords={"rust": 5.0},
    )
    assert [i.title for i in boosted.sections[0].items] == [
        "Deep dive on Rust",
        "Weekly newsletter",
    ]


def test_assemble_digest_empty_returns_no_sections() -> None:
    digest = assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[],
        now=NOW,
    )
    assert digest.sections == ()
    assert digest.is_empty


# ---------------------------------------------------------------------------
# Renderers — snapshot tests
# ---------------------------------------------------------------------------


def test_render_html_matches_snapshot() -> None:
    rendered = render_html(_canned_digest())
    _snapshot(rendered, "canned.html")


def test_render_markdown_matches_snapshot() -> None:
    rendered = render_markdown(_canned_digest())
    _snapshot(rendered, "canned.md")


def test_render_html_empty_matches_snapshot() -> None:
    empty = Digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sections=(),
    )
    _snapshot(render_html(empty), "empty.html")


def test_render_markdown_empty_matches_snapshot() -> None:
    empty = Digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sections=(),
    )
    _snapshot(render_markdown(empty), "empty.md")


def test_render_html_escapes_hostile_content() -> None:
    src = _source(id=1, title="<script>alert(1)</script>", url="https://evil.example/")
    sig = _signal(
        id=1,
        source_id=1,
        title="A <b>bold</b> claim",
        url="https://evil.example/story",
        summary='"quoted" & <em>emphasized</em>',
        published_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )
    digest = assemble_digest(
        user_email="alice@example.com",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sources_with_signals=[(src, [sig])],
        now=NOW,
    )
    html = render_html(digest)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A &lt;b&gt;bold&lt;/b&gt; claim" in html
    assert "&amp;" in html


# ---------------------------------------------------------------------------
# build_digest — DB-backed integration
# ---------------------------------------------------------------------------


def test_build_digest_queries_user_signals_end_to_end(session: Session) -> None:
    user = UserRepository(session).create(email="bob@example.com", hashed_password="x")
    session.commit()
    src_repo = SourceRepository(session)
    sig_repo = SignalRepository(session)
    source = src_repo.create(user_id=user.id, url="https://feed.example/", title="Feed")
    other = src_repo.create(user_id=user.id, url="https://other.example/", title="Other feed")
    session.commit()

    in_window = sig_repo.create(
        source_id=source.id,
        guid="a",
        title="In window",
        url="https://feed.example/a",
        summary="Fresh news.",
        published_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
    )
    sig_repo.create(
        source_id=source.id,
        guid="b",
        title="Before window",
        url="https://feed.example/b",
        summary=None,
        published_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
    )
    sig_repo.create(
        source_id=other.id,
        guid="c",
        title="Other in window",
        url="https://other.example/c",
        summary=None,
        published_at=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
    )
    session.commit()

    digest = build_digest(
        session,
        user,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW,
    )

    all_titles = {item.title for section in digest.sections for item in section.items}
    assert all_titles == {"In window", "Other in window"}
    # The section ordering surfaces the source with the freshest item first.
    assert digest.sections[0].items[0].title == in_window.title
    assert digest.user_email == "bob@example.com"


def test_build_digest_for_user_with_no_sources_returns_empty(session: Session) -> None:
    user = UserRepository(session).create(email="lonely@example.com", hashed_password="x")
    session.commit()

    digest = build_digest(
        session,
        user,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW,
    )
    assert digest.is_empty
    assert digest.sections == ()
