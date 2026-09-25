"""Static source registry and shared Core-table definitions.

The editorial pipeline pulls from a fixed, checked-in list of feeds rather
than accepting user-added sources. This module owns:

* :data:`CATEGORY_HINTS` and :data:`SOURCE_KINDS` — the closed vocabularies
  the YAML file is validated against.
* :func:`load_sources_yaml` — parse and validate ``sources.yaml``.
* :func:`upsert_sources` / :func:`upsert_sources_from_yaml` — write the
  parsed specs into the ``sources`` table, updating rows in place when the
  URL already exists so re-running the loader is idempotent.
* :data:`sources_table` / :data:`raw_items_table` / :data:`clusters_table` /
  :data:`issues_table` / :data:`items_table` — SQLAlchemy Core tables that
  mirror the columns created by migration ``0003_curated_digest_schema``. The
  ingest, build, and CLI layers all read and write through these Core
  definitions; there is no declarative ORM base for the curated pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    or_,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED_SOURCES_YAML = Path(__file__).resolve().parent / "data" / "sources.yaml"
_REPO_SOURCES_YAML = REPO_ROOT / "sources.yaml"
# Prefer the copy shipped inside the package (present in the built image);
# fall back to the repo-root file during local development.
DEFAULT_SOURCES_YAML = (
    _PACKAGED_SOURCES_YAML if _PACKAGED_SOURCES_YAML.exists() else _REPO_SOURCES_YAML
)

CATEGORY_HINTS: frozenset[str] = frozenset(
    {
        "models",
        "lawsuits_policy",
        "funding",
        "research",
        "industry_moves",
    }
)

SOURCE_KINDS: frozenset[str] = frozenset({"rss", "atom", "arxiv_rss"})

# Source kinds whose items can only ever belong to one section, whatever the
# YAML says: arXiv listings are research papers, full stop.
ALWAYS_LOCKED_KINDS: frozenset[str] = frozenset({"arxiv_rss"})


def is_category_locked(kind: str | None, category_locked: bool | None) -> bool:
    """Whether a source's ``category_hint`` should override keyword matches."""
    return bool(category_locked) or kind in ALWAYS_LOCKED_KINDS


sources_metadata = MetaData()

sources_table = Table(
    "sources",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("url", String(2048), nullable=False, unique=True, index=True),
    Column("kind", String(32), nullable=False),
    Column("category_hint", String(64), nullable=True),
    # Publication name shown on each item's byline (``sources.yaml`` ``name``).
    # NULL when unset; readers fall back to the feed host.
    Column("name", String(255), nullable=True),
    # When set, the classifier uses ``category_hint`` outright instead of
    # letting headline keywords override it — for clearly single-category
    # feeds (arXiv, court dockets). Mirrors migration 0007.
    Column(
        "category_locked",
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    ),
    Column("active", Boolean, nullable=False, default=True, server_default="1"),
    # Health counters maintained by the ingest layer and consumed by
    # :mod:`signalweek.ingest.health` to prune dead/silent sources.
    Column(
        "consecutive_fetch_failures",
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    ),
    Column("last_fetch_ok_at", DateTime(timezone=True), nullable=True),
    Column("last_fetch_error_at", DateTime(timezone=True), nullable=True),
    Column("last_item_at", DateTime(timezone=True), nullable=True),
    Column("deactivated_at", DateTime(timezone=True), nullable=True),
    Column("deactivation_reason", String(64), nullable=True),
)

# Append-only audit log of every activation/deactivation the health prune
# step performs. ``action`` is either ``'activated'`` or ``'deactivated'``,
# ``reason`` is a short machine-readable tag (``fetch_failures``, ``silent``,
# ``recovered``). Mirrors the ``source_health_events`` table created by
# migration 0005.
source_health_events_table = Table(
    "source_health_events",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "source_id",
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("at", DateTime(timezone=True), nullable=False, index=True),
    Column("action", String(16), nullable=False),
    Column("reason", String(64), nullable=False),
    CheckConstraint(
        "action IN ('activated', 'deactivated')",
        name="ck_source_health_events_action",
    ),
)

# Raw articles/posts ingested from each source, before clustering/summarization.
# Mirrors the ``raw_items`` table created by migration 0003.
raw_items_table = Table(
    "raw_items",
    sources_metadata,
    Column(
        "id",
        Integer,
        primary_key=True,
    ),
    Column(
        "source_id",
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("url", String(2048), nullable=False),
    Column("canonical_url", String(2048), nullable=False, index=True),
    Column("title", String(1024), nullable=False),
    Column("body", Text, nullable=True),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, index=True),
    # The feed entry's own ``published``/``updated`` stamp; NULL when undated.
    Column("published_at", DateTime(timezone=True), nullable=True),
    # Set by the clustering pass; NULL until the item has been clustered.
    Column(
        "cluster_id",
        Integer,
        ForeignKey("clusters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    ),
    # float32 sentence embedding of ``title`` (see signalweek.ingest.embed),
    # computed once so later ticks never re-embed old headlines.
    Column("title_embedding", LargeBinary, nullable=True),
    UniqueConstraint("source_id", "canonical_url", name="uq_raw_items_source_canonical"),
)

# Dedup groups of raw_items that all cover the same story. The clustering pass
# in :mod:`signalweek.ingest.cluster` upserts rows here as it groups incoming
# raw_items (membership lives in ``raw_items.cluster_id``). ``primary_url`` and
# ``canonical_headline`` come from the earliest
# (by ``first_seen_at``) raw_item in the group.
# Mirrors the ``clusters`` table created by migration 0003.
clusters_table = Table(
    "clusters",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("primary_url", String(2048), nullable=False, index=True),
    Column("category", String(64), nullable=False, index=True),
    Column("canonical_headline", String(1024), nullable=False),
)

# One row per weekly issue of the digest. ``status`` moves ``draft`` → ``held``
# (fewer than the minimum item count) or ``draft`` → ``published`` (a full
# issue). ``week_of`` is the Monday of the ISO week the issue covers.
# Mirrors the ``issues`` table created by migration 0003.
issues_table = Table(
    "issues",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("week_of", Date, nullable=False),
    Column("status", String(16), nullable=False, server_default="draft"),
    Column("published_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('draft', 'held', 'published')", name="ck_issues_status"),
    UniqueConstraint("week_of", name="uq_issues_week_of"),
)

# One row per item placed into an issue: a categorised, ordered story with a
# rule-based summary and a primary source URL. ``extra_source_urls`` is the
# ordered list of other outlets whose raw_items fell into the same cluster.
# Mirrors the ``items`` table created by migration 0003.
items_table = Table(
    "items",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "issue_id",
        Integer,
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "cluster_id",
        Integer,
        ForeignKey("clusters.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    Column("category", String(64), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("headline", String(1024), nullable=False),
    Column("summary", Text, nullable=False),
    Column("primary_url", String(2048), nullable=False),
    Column("extra_source_urls", JSON, nullable=False, server_default="[]"),
    # Byline for the primary source (migration 0008): publication name and
    # the source's publish date (entry date, else when we first saw it).
    Column("source_name", String(255), nullable=True),
    Column("source_published_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("issue_id", "position", name="uq_items_issue_position"),
    UniqueConstraint("issue_id", "cluster_id", name="uq_items_issue_cluster"),
)


# Reason codes an ``alerts`` row may carry. ``insufficient_items``: a week
# ended below the publish floor (spec criterion 15) and was held.
# ``missed_run``: a weekly slot passed with no issue built. ``pipeline_failed``:
# a scheduled job raised. Mirrors the table created by migration 0006.
ALERT_REASONS: tuple[str, ...] = ("insufficient_items", "missed_run", "pipeline_failed")

alerts_table = Table(
    "alerts",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("reason", String(32), nullable=False, index=True),
    Column("job", String(32), nullable=True),
    Column("week_of", Date, nullable=True),
    Column("detail", Text, nullable=True),
    CheckConstraint(
        "reason IN ('insufficient_items', 'missed_run', 'pipeline_failed')",
        name="ck_alerts_reason",
    ),
)

# One row per scheduled job execution (hourly ingest, weekly pipeline) so
# every run is measurable (spec criterion 16). ``item_count`` is the number
# of raw_items inserted for ingest and the published item count for the
# weekly pipeline. Mirrors the table created by migration 0006.
pipeline_runs_table = Table(
    "pipeline_runs",
    sources_metadata,
    Column("id", Integer, primary_key=True),
    Column("job", String(32), nullable=False, index=True),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", String(16), nullable=False),
    Column("item_count", Integer, nullable=True),
    Column("week_of", Date, nullable=True),
    Column("detail", Text, nullable=True),
    CheckConstraint(
        "status IN ('running', 'ok', 'failed', 'skipped')",
        name="ck_pipeline_runs_status",
    ),
)


@dataclass(frozen=True)
class SourceSpec:
    """A single entry from ``sources.yaml`` after validation."""

    url: str
    kind: str
    category_hint: str
    name: str | None = None
    category_locked: bool = False


class SourceRegistryError(ValueError):
    """Raised when ``sources.yaml`` is malformed or contains invalid values."""


def load_sources_yaml(path: str | Path | None = None) -> list[SourceSpec]:
    """Read and validate the source registry from disk.

    A missing ``sources:`` key, duplicate URLs, unknown ``kind``/
    ``category_hint`` values, or non-string URLs all raise
    :class:`SourceRegistryError` — the loader refuses to silently drop
    entries so a typo in the YAML fails loudly at boot instead of at
    publication time.
    """
    resolved = Path(path) if path is not None else DEFAULT_SOURCES_YAML
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceRegistryError(f"could not read {resolved}: {exc}") from exc

    data = yaml.safe_load(raw_text)
    return _parse_document(data, source=str(resolved))


def _parse_document(data: Any, *, source: str) -> list[SourceSpec]:
    if not isinstance(data, dict) or "sources" not in data:
        raise SourceRegistryError(f"{source}: expected a mapping with a 'sources' key")
    entries = data["sources"]
    if not isinstance(entries, list) or not entries:
        raise SourceRegistryError(f"{source}: 'sources' must be a non-empty list")

    specs: list[SourceSpec] = []
    seen_urls: set[str] = set()
    for index, entry in enumerate(entries):
        spec = _parse_entry(entry, source=source, index=index)
        if spec.url in seen_urls:
            raise SourceRegistryError(f"{source}: duplicate url {spec.url!r} at index {index}")
        seen_urls.add(spec.url)
        specs.append(spec)
    return specs


def _parse_entry(entry: Any, *, source: str, index: int) -> SourceSpec:
    where = f"{source}: entry #{index}"
    if not isinstance(entry, dict):
        raise SourceRegistryError(f"{where}: expected a mapping, got {type(entry).__name__}")

    url = entry.get("url")
    kind = entry.get("kind")
    category_hint = entry.get("category_hint")
    name = entry.get("name")

    if not isinstance(url, str) or not url.strip():
        raise SourceRegistryError(f"{where}: 'url' must be a non-empty string")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise SourceRegistryError(
            f"{where}: 'kind' must be one of {sorted(SOURCE_KINDS)}, got {kind!r}"
        )
    if not isinstance(category_hint, str) or category_hint not in CATEGORY_HINTS:
        raise SourceRegistryError(
            f"{where}: 'category_hint' must be one of {sorted(CATEGORY_HINTS)}, "
            f"got {category_hint!r}"
        )
    if name is not None and not isinstance(name, str):
        raise SourceRegistryError(f"{where}: 'name', if given, must be a string")
    category_locked = entry.get("category_locked", False)
    if not isinstance(category_locked, bool):
        raise SourceRegistryError(f"{where}: 'category_locked', if given, must be a boolean")

    return SourceSpec(
        url=url.strip(),
        kind=kind,
        category_hint=category_hint,
        name=name.strip() if isinstance(name, str) else None,
        category_locked=category_locked,
    )


@dataclass(frozen=True)
class UpsertResult:
    """Summary of an :func:`upsert_sources` run."""

    inserted: int
    updated: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


def upsert_sources(
    bind: Session | Connection,
    specs: Iterable[SourceSpec],
) -> UpsertResult:
    """Insert or update rows in ``sources`` from ``specs``.

    An entry whose ``url`` already exists has its ``kind``, ``category_hint``,
    ``category_locked`` and ``active`` flags brought back into line with the YAML — this is how a
    hint reclassification or a temporarily-disabled source flips back on.
    Rows already present but not mentioned in ``specs`` are left untouched;
    retiring a source is a separate, deliberate operation.
    """
    connection = _as_connection(bind)

    inserted = 0
    updated = 0
    unchanged = 0

    for spec in specs:
        existing = connection.execute(
            select(
                sources_table.c.id,
                sources_table.c.kind,
                sources_table.c.category_hint,
                sources_table.c.category_locked,
                sources_table.c.active,
                sources_table.c.name,
            ).where(sources_table.c.url == spec.url)
        ).first()
        locked = is_category_locked(spec.kind, spec.category_locked)

        if existing is None:
            connection.execute(
                sources_table.insert().values(
                    url=spec.url,
                    kind=spec.kind,
                    category_hint=spec.category_hint,
                    category_locked=locked,
                    active=True,
                    name=spec.name,
                )
            )
            inserted += 1
            continue

        needs_update = (
            existing.kind != spec.kind
            or existing.category_hint != spec.category_hint
            or bool(existing.category_locked) is not locked
            or bool(existing.active) is not True
            or existing.name != spec.name
        )
        if needs_update:
            connection.execute(
                sources_table.update()
                .where(sources_table.c.id == existing.id)
                .values(
                    kind=spec.kind,
                    category_hint=spec.category_hint,
                    category_locked=locked,
                    active=True,
                    name=spec.name,
                )
            )
            updated += 1
        else:
            unchanged += 1

    return UpsertResult(inserted=inserted, updated=updated, unchanged=unchanged)


def seed_sources_if_empty(bind: Session | Connection, path: str | Path | None = None) -> int:
    """Seed the registry from the packaged ``sources.yaml`` the first time the
    app boots against an empty ``sources`` table. Idempotent: a no-op when any
    source already exists. Returns the number of sources seeded.

    This exists so production boots with a working source list instead of an
    empty pipeline — the unit loaders alone never run unless something calls
    them, which is exactly the wiring gap this closes."""
    conn = _as_connection(bind)
    already = conn.execute(select(sources_table.c.id).limit(1)).first()
    if already is not None:
        return 0
    result = upsert_sources_from_yaml(bind, path)
    return getattr(result, "total", 0) or 0


def sync_source_names(bind: Session | Connection, path: str | Path | None = None) -> int:
    """Copy each YAML ``name`` onto its existing ``sources`` row.

    Touches nothing else — unlike :func:`upsert_sources` it never re-activates
    a source the health prune retired. Runs at boot so bylines follow the
    registry. Returns the number of rows changed."""
    conn = _as_connection(bind)
    changed = 0
    for spec in load_sources_yaml(path):
        if spec.name is None:
            continue
        result = conn.execute(
            sources_table.update()
            .where(
                sources_table.c.url == spec.url,
                or_(sources_table.c.name.is_(None), sources_table.c.name != spec.name),
            )
            .values(name=spec.name)
        )
        changed += result.rowcount or 0
    return changed


def upsert_sources_from_yaml(
    bind: Session | Connection,
    path: str | Path | None = None,
) -> UpsertResult:
    """Convenience: parse ``sources.yaml`` and upsert its contents."""
    return upsert_sources(bind, load_sources_yaml(path))


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
