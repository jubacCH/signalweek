"""Feed ingestion adapters."""

from __future__ import annotations

from signalweek.ingest.github import (
    GITHUB_TRENDING_URL,
    fetch_github_trending,
    ingest_github_trending,
    parse_github_trending,
)
from signalweek.ingest.hn import (
    HN_SEARCH_URL,
    fetch_hn_search,
    ingest_hn,
    parse_hn_hits,
)
from signalweek.ingest.rss import (
    IngestResult,
    ParsedEntry,
    canonicalize_url,
    fetch_feed,
    ingest_feed,
    parse_feed,
)

__all__ = [
    "GITHUB_TRENDING_URL",
    "HN_SEARCH_URL",
    "IngestResult",
    "ParsedEntry",
    "canonicalize_url",
    "fetch_feed",
    "fetch_github_trending",
    "fetch_hn_search",
    "ingest_feed",
    "ingest_github_trending",
    "ingest_hn",
    "parse_feed",
    "parse_github_trending",
    "parse_hn_hits",
]
