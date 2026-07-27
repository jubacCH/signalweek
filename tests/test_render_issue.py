"""Tests for the fixed 5-section issue renderer.

The renderer takes an issue's metadata plus a flat list of items and
returns HTML for the public issue page. These tests pin the contract:

* All five taxonomy sections always render, in fixed order.
* Each item shows headline, summary, primary source link, and extras.
* Untrusted text is HTML-escaped.
* The bespoke signalweek design system stays wired in through base chrome.
"""

from __future__ import annotations

import html as html_lib
from datetime import UTC, date, datetime
from typing import Any

import pytest

from signalweek.ingest.classify import CATEGORIES, CATEGORY_LABELS
from signalweek.web.renderers import render_issue

WEEK_OF = date(2026, 7, 27)
PUBLISHED_AT = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def _item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "models",
        "position": 1,
        "headline": "OpenAI releases GPT-6",
        "summary": "OpenAI unveiled GPT-6 today with a 2M-token context window.",
        "primary_url": "https://openai.com/blog/gpt6",
        "extra_source_urls": [],
    }
    base.update(overrides)
    return base


def _render(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "week_of": WEEK_OF,
        "status": "published",
        "published_at": PUBLISHED_AT,
        "items": [_item()],
    }
    kwargs.update(overrides)
    return render_issue(**kwargs)


def test_all_five_section_labels_render() -> None:
    text = html_lib.unescape(_render())
    for label in CATEGORY_LABELS.values():
        assert label in text


def test_sections_render_in_fixed_taxonomy_order() -> None:
    text = html_lib.unescape(
        _render(items=[_item(category=cat, headline=f"H-{cat}") for cat in CATEGORIES])
    )
    positions = [text.index(CATEGORY_LABELS[cat]) for cat in CATEGORIES]
    assert positions == sorted(positions), (
        f"section labels not in fixed order; got positions {positions}"
    )


def test_item_shows_headline_summary_and_primary_link() -> None:
    html = _render(
        items=[
            _item(
                category="funding",
                headline="Acme raises Series C",
                summary="Acme raised $200M in a Series C led by Sequoia.",
                primary_url="https://acme.example/press-release",
            )
        ]
    )
    assert "Acme raises Series C" in html
    assert "Acme raised $200M in a Series C led by Sequoia." in html
    assert 'href="https://acme.example/press-release"' in html


def test_extra_source_urls_are_rendered_as_links() -> None:
    html = _render(
        items=[
            _item(
                extra_source_urls=[
                    "https://one.example/feed",
                    "https://two.example/feed",
                ]
            )
        ]
    )
    assert 'href="https://one.example/feed"' in html
    assert 'href="https://two.example/feed"' in html
    # An "also covered by" affordance surfaces the extras section.
    assert "Also covered by" in html


def test_missing_extra_sources_do_not_render_also_covered_by() -> None:
    html = _render(items=[_item(extra_source_urls=[])])
    assert "Also covered by" not in html


def test_headline_and_summary_are_html_escaped() -> None:
    html = _render(
        items=[
            _item(
                headline="<script>alert(1)</script>",
                summary="<img src=x onerror=alert(1)>",
            )
        ]
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img" in html


def test_items_sorted_by_position_within_category() -> None:
    html = _render(
        items=[
            _item(category="models", position=3, headline="third-headline"),
            _item(category="models", position=1, headline="first-headline"),
            _item(category="models", position=2, headline="second-headline"),
        ]
    )
    i1 = html.index("first-headline")
    i2 = html.index("second-headline")
    i3 = html.index("third-headline")
    assert i1 < i2 < i3


def test_empty_categories_render_heading_with_placeholder() -> None:
    text = html_lib.unescape(
        _render(status="held", published_at=None, items=[_item(category="models")])
    )
    # All five headings render even when only one category has items.
    for label in CATEGORY_LABELS.values():
        assert label in text
    # Empty sections carry a visible placeholder for editorial review.
    assert "Nothing in this section this week." in text


def test_published_status_shows_published_datetime() -> None:
    html = _render()
    assert 'datetime="2026-07-27T09:00:00+00:00"' in html
    assert "Published" in html


def test_held_status_marks_the_page_as_preview() -> None:
    html = _render(status="held", published_at=None)
    assert "Preview" in html
    assert "held" in html


def test_unknown_category_is_rejected() -> None:
    with pytest.raises(ValueError):
        render_issue(
            week_of=WEEK_OF,
            status="published",
            published_at=PUBLISHED_AT,
            items=[_item(category="ai_moves")],
        )


def test_issue_page_uses_signalweek_design_system() -> None:
    html = _render()
    # base.html.j2 chrome + bespoke design system are wired through.
    assert "signalweek.css" in html
    assert "pico.min.css" in html
    # #digest-permalink is the styled wrapper for issue pages.
    assert 'id="digest-permalink"' in html


def test_week_of_appears_in_heading() -> None:
    html = _render()
    assert "2026-07-27" in html
