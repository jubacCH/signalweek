"""Feed ingestion adapters."""

from __future__ import annotations

from signalweek.ingest.rss import (
    IngestResult,
    ParsedEntry,
    canonicalize_url,
    fetch_feed,
    ingest_feed,
    parse_feed,
)

__all__ = [
    "IngestResult",
    "ParsedEntry",
    "canonicalize_url",
    "fetch_feed",
    "ingest_feed",
    "parse_feed",
]
