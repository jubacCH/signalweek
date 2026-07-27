"""Source ingestion: fetch active feeds and materialize them as raw_items rows."""

from signalweek.ingest.canonical import canonicalize_url
from signalweek.ingest.classify import (
    CATEGORIES,
    CATEGORY_LABELS,
    FALLBACK_CATEGORY,
    ClassifyRunResult,
    classify_clusters,
    classify_text,
)
from signalweek.ingest.cluster import ClusterRunResult, cluster_raw_items
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
    "CATEGORIES",
    "CATEGORY_LABELS",
    "FALLBACK_CATEGORY",
    "ClassifyRunResult",
    "ClusterRunResult",
    "FetchError",
    "FetchedEntry",
    "IngestRunResult",
    "SourceIngestResult",
    "canonicalize_url",
    "classify_clusters",
    "classify_text",
    "cluster_raw_items",
    "fetch_feed",
    "ingest_all_active",
    "ingest_source",
    "parse_feed",
]
