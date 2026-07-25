"""Source ingestion: fetch external feeds and materialize them as Signal rows."""

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.feeds import (
    FetchedEntry,
    FetchError,
    fetch_feed,
    ingest_source,
    parse_feed,
)
from signalweek.ingest.hackernews import (
    HackerNewsError,
    HackerNewsHit,
    fetch_hn,
    ingest_hn_source,
    parse_hn,
)

__all__ = [
    "FetchError",
    "FetchedEntry",
    "HackerNewsError",
    "HackerNewsHit",
    "canonicalize_url",
    "fetch_feed",
    "fetch_hn",
    "ingest_hn_source",
    "ingest_source",
    "parse_feed",
    "parse_hn",
]
