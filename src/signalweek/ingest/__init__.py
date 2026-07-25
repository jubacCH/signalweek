"""Source ingestion: fetch external feeds and materialize them as Signal rows."""

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.feeds import (
    FetchedEntry,
    FetchError,
    fetch_feed,
    ingest_source,
    parse_feed,
)

__all__ = [
    "FetchError",
    "FetchedEntry",
    "canonicalize_url",
    "fetch_feed",
    "ingest_source",
    "parse_feed",
]
