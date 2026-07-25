"""Tests for :mod:`signalweek.web.validate`.

Network I/O is driven with ``httpx.MockTransport`` so no real HTTP happens.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from signalweek.web.validate import (
    FeedValidationError,
    ValidatedFeed,
    validate_feed_url,
)

RSS_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel>'
    b"<title>Example Feed</title>"
    b"<link>https://example.com/</link>"
    b"<description>An example feed.</description>"
    b"<item><title>Hello</title><link>https://example.com/hello</link></item>"
    b"</channel></rss>"
)

ATOM_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<feed xmlns="http://www.w3.org/2005/Atom">'
    b"<title>Atom Feed</title>"
    b"<id>urn:uuid:1</id>"
    b"<entry><title>One</title><id>1</id>"
    b'<link href="https://example.com/one"/></entry>'
    b"</feed>"
)


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_validate_accepts_valid_rss_feed() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, content=RSS_BODY)

    with _mock_client(handler) as client:
        result = validate_feed_url("https://example.com/feed.xml", client=client)

    assert isinstance(result, ValidatedFeed)
    assert result.url == "https://example.com/feed.xml"
    assert result.title == "Example Feed"
    assert result.feed_type == "rss"
    # A HEAD probe runs before the GET probe.
    assert calls[0][0] == "HEAD"
    assert calls[1][0] == "GET"


def test_validate_accepts_valid_atom_feed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ATOM_BODY)

    with _mock_client(handler) as client:
        result = validate_feed_url("https://example.com/atom.xml", client=client)

    assert result.feed_type == "atom"
    assert result.title == "Atom Feed"


def test_validate_tolerates_head_not_implemented() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, content=RSS_BODY)

    with _mock_client(handler) as client:
        result = validate_feed_url("https://example.com/feed.xml", client=client)

    assert result.title == "Example Feed"


def test_validate_rejects_non_http_scheme() -> None:
    with pytest.raises(FeedValidationError, match="http://"):
        validate_feed_url("ftp://example.com/feed.xml")


def test_validate_rejects_missing_host() -> None:
    with pytest.raises(FeedValidationError):
        validate_feed_url("https:///no-host")


def test_validate_rejects_empty_url() -> None:
    with pytest.raises(FeedValidationError, match="Please enter"):
        validate_feed_url("   ")


def test_validate_rejects_unreachable_url() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _mock_client(handler) as client, pytest.raises(FeedValidationError, match="reach"):
        validate_feed_url("https://example.com/feed.xml", client=client)


def test_validate_rejects_http_error_on_head() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _mock_client(handler) as client, pytest.raises(FeedValidationError, match="500"):
        validate_feed_url("https://example.com/feed.xml", client=client)


def test_validate_rejects_html_response_that_is_not_a_feed() -> None:
    html = b"<!doctype html><html><body><h1>Hi</h1></body></html>"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html)

    with _mock_client(handler) as client, pytest.raises(FeedValidationError, match="feed"):
        validate_feed_url("https://example.com/notfeed", client=client)


def test_validate_rejects_get_failure_after_ok_head() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(404)

    with _mock_client(handler) as client, pytest.raises(FeedValidationError):
        validate_feed_url("https://example.com/feed.xml", client=client)


def test_validate_strips_surrounding_whitespace_in_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=RSS_BODY)

    with _mock_client(handler) as client:
        result = validate_feed_url("  https://example.com/feed.xml  ", client=client)

    assert result.url == "https://example.com/feed.xml"
    assert seen[0] == "https://example.com/feed.xml"
