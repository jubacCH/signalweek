"""Source ingestion: fetch active feeds and materialize them as raw_items rows."""

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.feeds import (
    FetchedEntry,
    FetchError,
    IngestRunResult,
    SourceIngestResult,
    fetch_feed,
    ingest_all_active,
    ingest_source,
    parse_feed,
)

__all__ = [
    "FetchError",
    "FetchedEntry",
    "IngestRunResult",
    "SourceIngestResult",
    "canonicalize_url",
    "fetch_feed",
    "ingest_all_active",
    "ingest_source",
    "parse_feed",
]
