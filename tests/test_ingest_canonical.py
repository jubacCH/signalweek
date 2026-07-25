"""Unit tests for URL canonicalization."""

from __future__ import annotations

import pytest

from signalweek.ingest.canonical import canonicalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (
            "HTTPS://Example.COM/Path/",
            "https://example.com/Path",
        ),
        (
            "https://example.com",
            "https://example.com/",
        ),
        (
            "https://example.com/a?b=2&a=1",
            "https://example.com/a?a=1&b=2",
        ),
        (
            "https://example.com/a?utm_source=x&utm_medium=y&keep=1",
            "https://example.com/a?keep=1",
        ),
        (
            "https://example.com/a#section",
            "https://example.com/a",
        ),
        (
            "https://example.com:443/a",
            "https://example.com/a",
        ),
        (
            "http://example.com:8080/a",
            "http://example.com:8080/a",
        ),
        (
            "  https://example.com/a  ",
            "https://example.com/a",
        ),
    ],
)
def test_canonicalize_url(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_canonicalize_url_stable_across_tracking_variants() -> None:
    """Two variants of the same article should collapse to the same key."""
    a = canonicalize_url("https://blog.example.com/posts/x?utm_source=twitter&fbclid=1")
    b = canonicalize_url("HTTPS://Blog.Example.com/posts/x/#comments")
    assert a == b == "https://blog.example.com/posts/x"
